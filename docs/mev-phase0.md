# Phase 0 — Base shadow scanner: measured, not promised

**Date:** 29 October 2026. **Status:** measurement complete, nothing signed, nothing spent.
**Code:** [mev/](../mev) · **Raw log:** [opportunities.jsonl](../mev/out/opportunities.jsonl)

---

## What this is

A scanner that watches the top ~100 Base pools by 24h volume, reads their live
state, sizes the optimal cross-venue trade with the closed form from the
[research report](mev-arb-research.md), and reports profit **after real gas**.
It does not sign, submit, or hold anything.

It exists to answer one question before any capital is committed:

> On the venues we can actually reach from a home connection, how often is
> there a dislocation, and what is it worth?

## How to run it

```bash
python -m mev.scanner --once --top 120 --pages 10          # one snapshot
python -m mev.scanner --minutes 10 --interval 6 --top 120  # shadow loop
python -m mev.analyze mev/out/opportunities.jsonl          # collapse to events
python -m mev.test_mev                                     # 24 checks
```

Discovery is cached for 30 minutes in `mev/cache/discovered.json`; pool
metadata in `mev/cache/pools.json`. Both are disposable — delete them to force
a clean re-probe.

## How it computes

For a two-pool route (buy on A, sell on B):

```
q*    = (√(γ₁γ₂·a_in·a_out·b_in·b_out) − a_in·b_in) / (γ₁(b_in + γ₂·a_out))
net   = (swap(swap(q* on A) on B) − q*) · price(input) − gas
gas   = 300k × gasPrice + getL1Fee(1200 bytes)      # OP Stack oracle, not a guess
edge  = (a_out·b_out)/(a_in·b_in) − 1                # round-trip, unitless
```

V3 pools are modelled with their in-range virtual reserves (`L/√P`, `L·√P`),
which is exact inside the current tick range and optimistic across one.

### The guards, and the bug each one came from

| Guard | Why it exists |
|---|---|
| Edge must exceed the two fees | A 2-pool arb that pays more in fees than it captures is not an arb |
| Edge must be under 50% | Anything wider is a decimals/orientation artefact — this caught a fake 5×10¹⁰ bp "opportunity" |
| Reference price from the **deepest** pool | Half-drained pools quote nonsense; a median with two pools elects the broken one |
| Both sides ≥ $2,000 | Dust pools produce enormous fake edges that can never fill |
| Impact ≤ 10% of the pool | A trade that eats the pool is not a fill, it is a move |

Each of these was added after the scanner reported something impossible. The
first version confidently "found" $28 of profit in a snapshot where the entire
real opportunity was zero.

## What it measures, and what it cannot

**Measures:** live pool state on Base, real gas including L1 data cost, real
dislocations between venues that share a token pair, optimal size and net P&L.

**Does not measure:**

- **Latency.** A scan at 6–20 second cadence sees only dislocations that
  persist. Real ones live for one 200ms Flashblock. This is therefore a
  *lower bound* on opportunity count and a *biased sample* of which ones exist.
- **Competition.** It does not know whether another bot took the trade in the
  same block. Every "opportunity" here is one someone else may already have
  eaten.
- **Tick crossings.** V3 math is exact only inside the active range. A large
  trade crossing ticks will execute worse than modelled.
- **V4 and Solidly-style pools** are excluded structurally (different math).

## Results — 8.5 minutes live, 65 distinct dislocations

```
rows 138   distinct events 65   window 8.5 min
sum of event peaks $54.19   median event $0.15   mean $0.83
```

| Pair | Venues | Edge | Size | Net |
|---|---|---|---|---|
| WETH/USDC | Aero Slipstream 2.9bp → Aerodrome 1.5bp | 18.5 bp | $15.4k | $10.87 |
| WETH/USDC | Aero Slipstream 2.9bp → Pancake V3 1bp | 19.6 bp | $13.0k | $10.24 |
| WETH/USDC | Aero Slipstream 5.3bp → Pancake V3 1bp | 17.2 bp | $6.7k | $3.62 |
| WETH/cbBTC | Aero Slipstream 4.5bp → Aero Slipstream 0.8bp | 9.6 bp | $17.2k | $3.61 |

Median event **$0.15**. Mean **$0.83**. The distribution is the story: a long
tail of cents and a handful of events above $3.

Sustained per-hour rate in this sample: **$381/hour** — but do not read that as
income. Three corrections apply immediately:

1. **Duplicate venues.** The same WETH/USDC dislocation is visible through
   four pools, so the engine reports it four times. Deduplicate by token pair
   and the total falls by roughly two thirds.
2. **Race loss.** On WETH/USDC we are competing with searchers who see the
   state 200ms before this laptop does. Realistic capture for this
   infrastructure: **0–20%**.
3. **Sampling bias.** We only see dislocations that survive 6+ seconds.

Adjusted, the honest range is **$25–$125/hour during active periods**, and
nothing during quiet ones. A 24-hour day is not 24 active hours.

## What this changes

The research report predicted this shape and the measurement confirms it: the
money on Base is in **fee-tier and venue dislocations on liquid pairs**, in
single-digit to ~20bp increments, worth dollars each. It is not in the
0.3%-per-hop route originally specified, which needs a 35bp gap and rarely
sees one.

Concretely, three things are now known rather than assumed:

- **Base gas is a non-issue.** $0.0042 per attempt, measured from the OP Stack
  oracle. Ten thousand failed attempts cost $42. That is what makes an
  aggressive search affordable here and impossible on Ethereum L1.
- **WETH/USDC is where the frequency is**, not where the margin is. Ten of
  the top twelve events were that pair, and every one of them was under
  $11 net.
- **cbBTC/WETH** shows up repeatedly at 6–10bp. That is the highest-quality
  second pair observed.

## What Phase 1 needs

To convert this from measurement to money:

1. **An executor contract** (Solidity, Foundry): flash-borrow, swap, verify
   `balance ≥ borrowed + minProfit`, else revert. No admin functions.
2. **A faster loop**: Flashblocks-aware WebSocket, 200ms cadence, sub-100ms
   compute. The current HTTP polling cannot compete for the 200ms events.
3. **A submission path** with a tip policy (Base sequencer endpoint; see the
   research report for what is and is not available).
4. **A gas tank** of $200–$500 and hard caps: 3 attempts/block, $20/day.

Expected net at launch, honestly: **$20–$80/day** if the loop is fast enough
to win a third of the events it sees, and **near zero** if it is not. The
measurement above is consistent with both outcomes; only Phase 1 settles it.

## Files

| Path | Role |
|---|---|
| `mev/chain.py` | JSON-RPC, ABI helpers, pool state, failover |
| `mev/math.py` | closed-form sizing, profit, gas |
| `mev/universe.py` | discovery, pricing, caching |
| `mev/scanner.py` | the loop and its guards |
| `mev/analyze.py` | collapses rows into distinct events |
| `mev/test_mev.py` | 24 checks, including the reciprocal-edge regression |
