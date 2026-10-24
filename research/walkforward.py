"""Walk-forward carry: rank on trailing funding only, hold forward, charge real fees."""
import json
import statistics as st

from carry import load, HOURS_YEAR

SPOT_FEE, PERP_FEE = 0.0025, 0.00045
RT = 2 * (SPOT_FEE + PERP_FEE)
MARGIN = 0.25


def run(series, coins, lookback_days=180, hold_days=90, slots=3, min_apr=0.0):
    timeline = sorted({t for c in coins for t, _, _ in series[c]})
    idx = {c: {t: (f, p) for t, f, p in series[c]} for c in coins}
    start_ms = timeline[0] + lookback_days * 86400 * 1000
    reb_ms = hold_days * 86400 * 1000

    equity = 1.0
    curve = []
    held = {}
    last_prem = {}
    fees = 0.0
    funding = 0.0
    next_reb = start_ms
    picks_log = []

    for t in timeline:
        if t < start_ms:
            continue
        for c in list(held):
            row = idx[c].get(t)
            if not row:
                continue
            f, p = row
            equity += held[c] * f
            funding += held[c] * f
            equity -= held[c] * (p - last_prem[c])
            last_prem[c] = p

        if t >= next_reb:
            next_reb = t + reb_ms
            scored = []
            for c in coins:
                rows = [r for r in series[c] if start_ms - lookback_days * 86400 * 1000 <= r[0] <= t]
                if len(rows) < lookback_days * 20:
                    continue
                a = st.mean(r[1] for r in rows[-lookback_days * 24:]) * HOURS_YEAR * 100
                scored.append((a, c))
            scored.sort(reverse=True)
            want = [c for a, c in scored[:slots] if a > min_apr]
            picks_log.append((t, [(c, round(a, 2)) for a, c in scored[:slots]]))
            for c in list(held):
                if c not in want:
                    n = held.pop(c)
                    fee = n * RT
                    fees += fee
                    equity -= fee
            w = 1.0 / slots
            for c in want:
                if c not in held and t in idx[c]:
                    fee = w * RT
                    fees += fee
                    equity -= fee
                    held[c] = w
                    last_prem[c] = idx[c][t][1]

        if t % (6 * 3600 * 1000) < 3600 * 1000:
            curve.append(equity)

    days = (timeline[-1] - start_ms) / 86400000
    cap_ret = (equity - 1.0) / (1 + MARGIN)
    ann = ((1 + cap_ret) ** (365 / days) - 1) * 100
    peak, dd = curve[0], 0.0
    for e in curve:
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak)
    rets = [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve))]
    vol = st.pstdev(rets) * (24 * 365 / 6) ** 0.5 * 100 if len(rets) > 2 else 0
    return {"days": days, "net_pct": (equity - 1) * 100, "ann_pct": ann, "dd_pct": dd * 100,
            "sharpe": ann / vol if vol else 0, "fees_pct": fees * 100, "funding_pct": funding * 100,
            "picks": picks_log, "curve": curve, "start_ms": start_ms}


def main():
    series = load()
    coins = sorted(series)
    print("=== walk-forward: rank on trailing 180d, hold 90d, 3 slots ===")
    r = run(series, coins)
    for k in ("days", "net_pct", "ann_pct", "dd_pct", "sharpe", "fees_pct", "funding_pct"):
        print(f"  {k:12} {r[k]:8.2f}")
    import time
    print("  picks:")
    for t, picks in r["picks"]:
        print("   ", time.strftime("%Y-%m-%d", time.gmtime(t / 1000)), picks)

    print("\n=== sensitivity (hold length / slots) ===")
    import itertools
    for hold, slots in itertools.product((30, 90, 180), (2, 3, 4)):
        x = run(series, coins, hold_days=hold, slots=slots)
        print(f"  hold {hold:3}d slots {slots}: ann {x['ann_pct']:6.2f}%  dd {x['dd_pct']:5.2f}%  "
              f"fees {x['fees_pct']:5.1f}%  sharpe {x['sharpe']:5.2f}")

    print("\n=== benchmark: always hold BTC+ETH ===")
    b = run(series, ["BTC", "ETH"], slots=2)
    print(f"  ann {b['ann_pct']:.2f}%  dd {b['dd_pct']:.2f}%  fees {b['fees_pct']:.1f}%")

    with open("walkforward.json", "w") as fh:
        json.dump({k: v for k, v in r.items() if k not in ("picks", "curve")}, fh, indent=1)


if __name__ == "__main__":
    main()
