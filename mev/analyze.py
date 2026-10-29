"""Turn a shadow-scan log into an honest answer: how many distinct
dislocations happened, and what was the best one worth?

A raw sum over log rows double-counts: the same 6bp gap persists across several
scans and can only be traded once. Rows are grouped into *events* — the same
pool pair, uninterrupted for more than `gap` seconds — and each event counts
once, at its peak observed profit.

    python -m mev.analyze mev/out/opportunities.jsonl
"""
from __future__ import annotations

import collections
import json
import sys
from datetime import datetime


def load(path: str) -> list[dict]:
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def group_events(rows: list[dict], gap: float = 150.0) -> list[dict]:
    """Collapse repeated sightings of one dislocation into one event."""
    by_key: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r in rows:
        key = (r["buy_pool"], r["sell_pool"], r["pair"])
        by_key[key].append(r)

    events = []
    for key, items in by_key.items():
        items.sort(key=lambda r: r["ts"])
        current = [items[0]]
        for nxt in items[1:]:
            prev_t = datetime.fromisoformat(current[-1]["ts"])
            next_t = datetime.fromisoformat(nxt["ts"])
            if (next_t - prev_t).total_seconds() > gap:
                events.append(_event(key, current))
                current = [nxt]
            else:
                current.append(nxt)
        events.append(_event(key, current))
    return sorted(events, key=lambda e: -e["peak_net"])


def _event(key: tuple, items: list[dict]) -> dict:
    peak = max(items, key=lambda r: r["net_usd"])
    return {
        "buy_pool": key[0], "sell_pool": key[1], "pair": key[2],
        "first": items[0]["ts"], "last": items[-1]["ts"],
        "sightings": len(items), "peak_net": peak["net_usd"],
        "peak_edge_bp": peak["edge_bp"], "peak_size_usd": peak["size_usd"],
        "buy_dex": peak["buy_dex"], "sell_dex": peak["sell_dex"],
        "buy_fee_bp": peak["buy_fee_bp"], "sell_fee_bp": peak["sell_fee_bp"],
    }


def main(argv=None):
    argv = argv or sys.argv[1:]
    path = argv[0] if argv else "mev/out/opportunities.jsonl"
    rows = load(path)
    if not rows:
        print(f"{path}: no opportunities logged")
        return

    events = group_events(rows)
    span = (datetime.fromisoformat(rows[-1]["ts"]) -
            datetime.fromisoformat(rows[0]["ts"])).total_seconds()
    total = sum(e["peak_net"] for e in events)

    print(f"rows {len(rows):,}   distinct events {len(events):,}   "
          f"window {span / 60:.1f} min")
    print(f"sum of event peaks ${total:.4f}   "
          f"median event ${sorted(e['peak_net'] for e in events)[len(events) // 2]:.4f}   "
          f"mean ${total / len(events):.4f}")
    print(f"implied rate ${total / max(span, 1) * 3600:.2f}/hour at this sampling cadence")
    print()
    print(f"{'pair':24s} {'venue':34s} {'edge':>7s} {'size':>10s} {'net':>9s}  n")
    for e in events[:12]:
        ven = f"{e['buy_dex'][:14]}{e['buy_fee_bp']:.1f} -> {e['sell_dex'][:10]}{e['sell_fee_bp']:.1f}"
        print(f"{e['pair']:24s} {ven:34s} {e['peak_edge_bp']:6.1f}b "
              f"{e['peak_size_usd']:>10,.0f} {e['peak_net']:>9.4f}  {e['sightings']}")

    by_pair = collections.Counter()
    for e in events:
        by_pair[e["pair"]] += e["peak_net"]
    print()
    for pair, net in by_pair.most_common(6):
        print(f"  {pair:24s} ${net:.4f}")


if __name__ == "__main__":
    main()
