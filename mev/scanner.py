"""Shadow arbitrage scanner for Base.

Reads live pool state, prices every pool pair that shares a token, sizes the
optimal trade with the closed form and reports profit *net of real gas*. It
never signs or submits anything — this is the measurement phase.

    python -m mev.scanner --once --top 60
    python -m mev.scanner --minutes 60 --interval 20 --top 80
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from .chain import BASE_RPC, STABLES, WETH, PoolState, Rpc, Universe
from .math import best_size, usd_gas
from .universe import load_pools, price_tokens

GAS_UNITS = 300_000          # flash loan + 2 swaps, planning envelope
TX_BYTES = 1_200             # serialized size used for the L1 fee query
OUT_DIR = "mev/out"


MIN_SIDE_USD = 2_000.0     # each leg needs real depth, not a dust pool
MAX_PRICE_SKEW = 2.0       # a pool quoting >2x away from its peers is broken
MAX_IMPACT = 0.10          # never model a fill that eats 10% of a pool
MAX_EDGE_BP = 5_000.0      # a >50% gap on one pair is a broken pool, not arb


def evaluate(states: list[PoolState], prices: dict[str, float], gas_usd: float,
             min_side_usd: float = MIN_SIDE_USD):
    """Every (pool pair, input token) candidate across the state snapshot."""
    by_pair: dict[tuple[str, str], list[PoolState]] = defaultdict(list)
    for st in states:
        by_pair[st.meta.pair].append(st)

    results = []
    for pair, pools in by_pair.items():
        if len(pools) < 2:
            continue
        pools = sorted(pools, key=lambda s: s.meta.address)

        # Sanity: compare every pool to the *deepest* one, never to the median.
        # Half-drained pools quote absurd prices, and with an even number of
        # pools a median happily elects the broken one as the reference.
        px0, px1 = prices.get(pair[0]), prices.get(pair[1])
        if not px0 or not px1:
            continue
        base = pair[0]

        def mid(state):
            r_base, r_quote = state.reserves_for(base)
            return r_quote / r_base if r_base > 0 else 0.0

        def depth(state):
            return min(state.reserve0 * px0, state.reserve1 * px1)

        ref = max(pools, key=depth)
        ref_mid = mid(ref)
        if ref_mid <= 0:
            continue
        pools = [s for s in pools if 0.5 < mid(s) / ref_mid < MAX_PRICE_SKEW]

        for i in range(len(pools)):
            for j in range(i + 1, len(pools)):
                a, b = pools[i], pools[j]
                for token_in in pair:
                    usd = prices.get(token_in)
                    usd_out = prices.get(pair[0] if token_in == pair[1] else pair[1])
                    if not usd or not usd_out:
                        continue
                    token_out = pair[0] if token_in == pair[1] else pair[1]
                    a_in, a_out = a.reserves_for(token_in)
                    b_in, b_out = b.reserves_for(token_out)
                    if min(a_in, a_out) * usd < min_side_usd or min(b_in, b_out) * usd < min_side_usd:
                        continue
                    sizing = best_size(a_in, a_out, b_in, b_out,
                                       a.meta.fee_bp, b.meta.fee_bp)
                    if not sizing or sizing.impact > MAX_IMPACT:
                        continue
                    # A real two-pool arb needs a mid-price gap wider than the
                    # fees it pays, and narrower than anything a sane pool can
                    # quote. Both bounds catch decimals/orientation artefacts.
                    if not (a.meta.fee_bp + b.meta.fee_bp) < sizing.edge_bp < MAX_EDGE_BP:
                        continue
                    gross = sizing.profit * usd
                    if gross < 0.0005:
                        continue
                    results.append({
                        "pair": f"{token_in[:8]}->{token_out[:8]}",
                        "buy": a.meta,
                        "sell": b.meta,
                        "sizing": sizing,
                        "token_in": token_in,
                        "usd_in": usd,
                        "buy_reserves": [a.reserve0, a.reserve1],
                        "sell_reserves": [b.reserve0, b.reserve1],
                        "gross_usd": gross,
                        "gas_usd": gas_usd,
                        "net_usd": gross - gas_usd,
                        "size_usd": sizing.q * usd,
                    })
    results.sort(key=lambda r: -r["net_usd"])
    return results, by_pair


def fmt_money(value: float) -> str:
    if abs(value) >= 1:
        return f"${value:,.2f}"
    if abs(value) >= 0.01:
        return f"${value:.4f}"
    return f"${value:.5f}"


def scan_once(rpc: Rpc, uni: Universe, top: int, pages: int, min_net: float,
              log_path: str | None) -> dict:
    pools = load_pools(uni, top=top, pages=pages)
    metas = [m for m, _ in pools]
    states = uni.states(metas)
    if not states:
        raise SystemExit("no pool state returned - RPC problem?")

    gas_price = rpc.gas_price_wei()
    l1_fee = rpc.l1_fee_wei(TX_BYTES)
    prices = price_tokens(states)
    eth = prices.get(WETH, 3_000.0)
    gas_usd = usd_gas(GAS_UNITS, gas_price, l1_fee, eth)
    block = rpc.block_number()

    results, by_pair = evaluate(states, prices, gas_usd)
    live = [r for r in results if r["net_usd"] >= min_net]

    print(f"\nblock {block:,}  |  {len(states)} pools  |  {len(by_pair)} token pairs  |  "
          f"gas {gas_price / 1e9:.4f} gwei + L1 {l1_fee / 1e18 * eth:.5f} USD "
          f"= {fmt_money(gas_usd)}/attempt")
    if live:
        print(f"{'pair':22s} {'buy':26s} {'sell':26s} {'edge':>7s} {'size':>10s} "
              f"{'gross':>9s} {'net':>9s} {'impact':>7s}")
        for r in live[:25]:
            s = r["sizing"]
            buy = f"{r['buy'].dex[:14]:14s} {r['buy'].fee_bp:>5.1f}bp"
            sell = f"{r['sell'].dex[:14]:14s} {r['sell'].fee_bp:>5.1f}bp"
            print(f"{r['pair']:22s} {buy:26s} {sell:26s} {s.edge_bp:>6.1f}b "
                  f"{fmt_money(r['size_usd']):>10s} {fmt_money(r['gross_usd']):>9s} "
                  f"{fmt_money(r['net_usd']):>9s} {s.impact * 100:>6.2f}%")
    else:
        near = results[0] if results else None
        extra = ""
        if near:
            extra = (f"  (best near-miss: {near['sizing'].edge_bp:.1f}bp edge, "
                     f"{fmt_money(near['gross_usd'])} gross)")
        print(f"no opportunity above {fmt_money(min_net)} net{extra}")

    if log_path:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(log_path, "a", encoding="utf-8") as fh:
            for r in live:
                fh.write(json.dumps({
                    "ts": stamp, "block": block, "pair": r["pair"],
                    "buy_pool": r["buy"].address, "sell_pool": r["sell"].address,
                    "buy_dex": r["buy"].dex, "sell_dex": r["sell"].dex,
                    "buy_fee_bp": r["buy"].fee_bp, "sell_fee_bp": r["sell"].fee_bp,
                    "edge_bp": round(r["sizing"].edge_bp, 2),
                    "size_usd": round(r["size_usd"], 2),
                    "gross_usd": round(r["gross_usd"], 6),
                    "gas_usd": round(r["gas_usd"], 6),
                    "net_usd": round(r["net_usd"], 6),
                    "impact_pct": round(r["sizing"].impact * 100, 3),
                    "buy_reserves": [round(x, 6) for x in r["buy_reserves"]],
                    "sell_reserves": [round(x, 6) for x in r["sell_reserves"]],
                }) + "\n")

    return {"block": block, "states": len(states), "pairs": len(by_pair),
            "live": live, "gas_usd": gas_usd, "results": results}


def summarize(history: list[dict]):
    if not history:
        return
    live = [r for h in history for r in h["live"]]
    total_net = sum(r["net_usd"] for r in live)
    total_gross = sum(r["gross_usd"] for r in live)
    print("\n" + "=" * 78)
    print(f"scans {len(history)}   windows with a live opportunity {sum(1 for h in history if h['live'])}")
    print(f"opportunities {len(live)}   gross {fmt_money(total_gross)}   "
          f"net {fmt_money(total_net)}   gas/attempt {fmt_money(history[-1]['gas_usd'])}")
    if live:
        sizes = [r["size_usd"] for r in live]
        edges = [r["sizing"].edge_bp for r in live]
        print(f"median size {fmt_money(statistics.median(sizes))}   "
              f"median edge {statistics.median(edges):.1f}bp   "
              f"max edge {max(edges):.1f}bp")
        by_pair = defaultdict(float)
        for r in live:
            by_pair[r["pair"]] += r["net_usd"]
        for pair, net in sorted(by_pair.items(), key=lambda kv: -kv[1])[:8]:
            print(f"  {pair:24s} {fmt_money(net)}")
    print("=" * 78)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Base cross-DEX arbitrage shadow scanner")
    ap.add_argument("--rpc", default=os.environ.get("BASE_RPC", BASE_RPC))
    ap.add_argument("--top", type=int, default=60, help="pools to watch")
    ap.add_argument("--pages", type=int, default=3, help="GeckoTerminal pages to discover from")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--minutes", type=float, default=0)
    ap.add_argument("--interval", type=float, default=20)
    ap.add_argument("--min-net", type=float, default=0.0)
    ap.add_argument("--log", default=os.path.join(OUT_DIR, "opportunities.jsonl"))
    args = ap.parse_args(argv)

    rpc = Rpc(args.rpc)
    uni = Universe(rpc)

    history = []
    started = time.time()
    try:
        while True:
            try:
                history.append(scan_once(rpc, uni, args.top, args.pages,
                                         args.min_net, args.log))
            except Exception as exc:  # noqa: BLE001 - a scan loop should survive
                print(f"scan failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            if args.once:
                break
            if args.minutes and (time.time() - started) / 60 >= args.minutes:
                break
            if not args.minutes and not args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    summarize(history)
    print(f"rpc calls {rpc.calls:,}  failed {rpc.failures:,}")


if __name__ == "__main__":
    main()
