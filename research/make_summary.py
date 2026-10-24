"""Bundle the carry research into research_summary.json for the dashboard."""
import json
import time

from carry import load
from carry_deploy import per_coin, rolling_negative
from walkforward import run

series = load()
coins = sorted(series)

stats = []
for c in coins:
    s = per_coin(series, c)
    rates = [r[1] for r in series[c]]
    stats.append({
        "coin": c,
        "mean_apr": sum(rates) / len(rates) * 8760 * 100,
        "positive_pct": 100 * sum(1 for r in rates if r > 0) / len(rates),
        "funding": s["funding"], "net": s["net"], "ann": s["ann"], "dd": s["dd"],
        "neg_spells": rolling_negative(series, c),
    })
stats.sort(key=lambda s: s["ann"], reverse=True)

wf = run(series, coins, slots=3, hold_days=90)
bench = run(series, ["BTC", "ETH"], slots=2, hold_days=90)
book = run(series, ["LINK", "BTC", "ETH"], slots=3, hold_days=90)

# Downsample the deployed-book replay to a curve the dashboard can draw.
curve = book["curve"]
step = max(1, len(curve) // 700)
points = [[book["start_ms"] + i * 6 * 3600 * 1000, round(e, 6)]
          for i, e in enumerate(curve) if i % step == 0]

summary = {
    "window": f"{time.strftime('%Y-%m-%d', time.gmtime(series['BTC'][0][0] / 1000))} to "
              f"{time.strftime('%Y-%m-%d', time.gmtime(series['BTC'][-1][0] / 1000))}",
    "hours": len(series["BTC"]),
    "coins": stats,
    "walkforward": {k: wf[k] for k in ("days", "net_pct", "ann_pct", "dd_pct", "sharpe", "fees_pct", "funding_pct")},
    "benchmark": {k: bench[k] for k in ("ann_pct", "dd_pct", "fees_pct")},
    "book_replay": {k: book[k] for k in ("days", "net_pct", "ann_pct", "dd_pct", "sharpe", "fees_pct", "funding_pct")},
    "curve": points,
    "fees": {"spot_pct": 0.25, "perp_pct": 0.045, "round_trip_pct": 0.59},
    "margin_pct": 0.25,
    "conclusion": ("Carry is structural, not tactical: the short leg is paid a median 10.95% APR "
                   "on Hyperliquid. Rotating into the highest-paying coin charged more in fees than "
                   "it added, so the live book holds a fixed basket and leaves only when funding turns."),
}

with open("research_summary.json", "w") as fh:
    json.dump(summary, fh, indent=1)
print(f"coins {len(stats)}  wf ann {wf['ann_pct']:.2f}%  book replay ann {book['ann_pct']:.2f}%  "
      f"curve points {len(points)}")
