# Ares — MEV / DEX Arbitrage: Research & Architecture

**Status:** research only. No code written. Snapshot date: 28 October 2026.
**Scope:** atomic (single-transaction) spatial arbitrage across Uniswap V2/V3 forks and SushiSwap on Arbitrum One and Base.

---

## 0. Verdict first

The strategy as specified — **WETH/USDC, Uniswap V3 vs SushiSwap, 0.3% per hop** — is structurally negative-EV and I would not build it. Not because the mechanism is wrong, but because the fee floor is above the typical dislocation:

| Route | Fees paid | Dislocation needed to break even |
|---|---|---|
| Uni V3 5bp → Sushi 30bp | 35 bp | > 35 bp, persistently, on the deepest pair on the chain |
| Uni V3 5bp → Uni V3 1bp | 6 bp | > 6 bp |
| Uni V3 1bp → Uni V3 1bp | 2 bp | > 2 bp |
| Long-tail 100bp → 30bp | 130 bp | > 130 bp (but these do happen, violently) |

On WETH/USDC the 30bp Sushi leg alone consumes more than the entire dislocation that exists in normal conditions. You would be paying 35bp to capture 8bp. Every backtest of that exact route that I have seen described in the literature, and the published measurements below, point the same way.

What *is* viable, and what this report designs, is a different cut of the same idea:

1. **Fee-tier arbitrage between Uniswap V3 pools** on the same pair (1bp vs 5bp vs 30bp). Cost per round trip 2–35bp, high frequency, deep liquidity.
2. **Long-tail coverage** — thin pools with 100bp/1% tiers where a single market order leaves a 300–2,000bp hole. Low frequency, high value per event, and the only place a new entrant without private orderflow has a real shot.
3. **Event-driven dislocations** — large swaps, oracle updates, liquidations, new pool launches, migrations. This is where the money actually is, and it is a *latency and coverage* business, not a signal business.

And the economic shape is not what most people expect: **arb profit scales with liquidity × dislocation², not with notional × edge.** A 25bp hole on a $20M pool is worth **$9**, not $5,000. That single fact drives every design decision below.

**Realistic expectation for a solo operator at launch:** single-digit dollars per captured opportunity on majors, $50–$500 on long-tail events, a low hit rate, and net profit in the hundreds of dollars per month until coverage and latency improve. Gas is cheap enough that failure is survivable (cents per revert), which makes the iterative approach viable — but there is no "paper mode" for gas, and no version of this prints green overnight.

---

## 1. Core strategy definition

### 1.1 What the edge actually is

An AMM quotes a price derived from its reserves, not from the market. When an external price moves (a CEX print, a large swap in another pool, a liquidation), every AMM holding that pair is temporarily wrong. The arbitrageur trades against the stale pool and closes the gap. In the literature this is the searcher side of **LVR** (loss-versus-rebalancing): the LP's loss is the arb's gross gain, minus fees and gas (Milionis, Moallemi, Roughgarden & Zhang).

Three consequences that matter for design:

- **The edge is created by other people's trades.** You cannot predict it; you can only be fast when it appears. This is a reactive system, not a forecasting system.
- **The edge decays in milliseconds.** On Base, a Flashblock lands every ~200ms; on Arbitrum, ordering rounds are 125ms. By the time a human sees a chart, it is gone.
- **Your gross edge is not your net.** Fees, gas, and the priority-fee auction each take a cut. In a priority-fee auction the winning bid converges on the value of the opportunity, which is precisely why contested opportunities pay ~nothing.

### 1.2 Market structure of the two target chains (2026)

This is the part most designs get wrong, because they assume Ethereum L1 mechanics.

| | Arbitrum One | Base |
|---|---|---|
| Block cadence | 250 ms nominal, 2× 125 ms ordering rounds | 2 s blocks, ~200 ms Flashblocks preconfirmations |
| Public mempool | **No.** Sequencer pool is private | **No.** Sequencer routes to a private pool |
| Ordering | Since the Oct 2026 change, **priority gas auction (PGA)** by effective priority fee, arrival time breaks ties; anti-starvation boost for old txs | Priority fee, but **arrival time matters**: Flashblock ordering locks once broadcast, a later higher tip cannot jump an already-committed tx |
| Early visibility | **Fast Feed** — paid, authenticated stream of *already ordered and executed* txs, ~$17/day minimum ticket, no replay, no state diff | Flashblocks-aware RPCs expose `pending` state and preconfirmed tx streams; raw stream reserved for node operators |
| Submission | `arb1-sequencer.arbitrum.io/rpc` (best-effort, no SLA) | `mainnet-sequencer.base.org` |
| Previous mechanism | Timeboost express lane (60 s auction, second-price, reserve raised to 0.0075 ETH in Feb 2026) — **retired** on Arbitrum One | — |

Three practical implications:

1. **There is nothing to sandwich you in a public mempool, because there is no public mempool.** The classic "get frontrun on Ethereum" fear does not transfer. What replaces it is a **race**: everyone who sees the same state change submits in the same 125–200 ms window, and the sequencer orders by bid.
2. **Fast Feed does not let you front-run.** It publishes transactions *after* ordering. It is a detection accelerant for the *next* opportunity, not a way to beat the current one.
3. **Flashbots Protect is Ethereum-only.** It is not available on Base or Arbitrum. Any design that assumes "submit through Protect" on an L2 is based on a false premise. The L2 equivalents are sequencer-direct submission, Flashblocks-aware RPCs, and OFA/protected routes (Alchemy MEV protection → Merkle/Blink covers both chains; bloXroute has Base fast RPC + streams with a 0.00008 ETH minimum tip, but its *Protect* endpoint is not documented for Base/Arbitrum).

### 1.3 Why WETH/USDC is the worst place to start, and the best place to prove yourself

WETH/USDC on Base or Arbitrum is where the professionals are: co-located, with private orderflow, running the same closed-form sizer you would run, on pools with $10M–$100M of liquidity. The dislocations are 1–10bp and last one block. You will lose that race with a Python process on a Raspberry Pi over home Wi-Fi, and you will pay gas to learn it.

But it is also the cleanest testbed: two assets, four pools, deep liquidity, no token weirdness, and a fee structure you can model exactly. **Use it for measurement, not for P&L.**

### 1.4 Where a new entrant can actually earn

Ranked by expected value per engineering hour for a solo operator:

| Segment | Edge size | Frequency | Competition | Verdict |
|---|---|---|---|---|
| Majors, V3 1bp vs 5bp | 2–15 bp | High (dozens/day) | Very high, latency-bound | Measurement layer; small P&L |
| Majors, V3 vs Sushi 30bp | 5–40 bp | Low | High | **Skip** — fee floor eats it |
| Long-tail V3 100bp/1% pools | 100–2,000 bp | Low (a few/day across 100+ tokens) | Medium; fewer bots watch them | **Primary P&L** |
| New pool launches / migrations | 500–5,000 bp | Rare, bursty | Medium, event-driven | High value, needs fast pool discovery |
| Liquidation-adjacent | 50–500 bp | Event-driven | High, dominated by SVR/Atlas on Base/Arb | Skip unless partnered |
| Triangular within one DEX | 3–30 bp | Medium | High | Cheap to add once the graph exists |
| Cross-chain (non-atomic) | Large | Medium | Medium | **Out of scope** — inventory and bridge risk |

The honest strategic read: **coverage beats speed for a small player.** Watching 400 pools and catching the 3 ugly ones nobody else monitors will out-earn trying to win the 5bp race on WETH/USDC. Speed matters only once you have something worth racing for.

### 1.5 Capital model

Atomic arb does not need inventory. Flash-borrow the input, swap, repay, keep the difference, revert if it does not clear:

| Provider | Fee | Notes |
|---|---|---|
| Balancer V2 Vault | **0%** | Documented as currently zero; repay in the same tx |
| Morpho Blue | **0%** | Singleton contract, callback repayment |
| Aave V3 | **0.05%** | Governance-mutable — read `FLASHLOAN_PREMIUM_TOTAL` on-chain, do not hardcode |
| Uniswap V3 `flash()` | Pool fee | Not free; `ceil(amount × fee / 1e6)` |

So the capital requirement is **gas only** — a hot wallet with $200–$500 of ETH is enough to trade size that is bounded by pool liquidity, not by your balance. That is the attractive part of this strategy: no inventory risk, no funding, no borrow/lend exposure. The costs that scale with notional are the swap fees and slippage, which are inside the optimizer.

---

## 2. Mathematical modelling and fee friction

### 2.1 Swap primitives

Constant-product pool with reserves (R_in, R_out) and fee multiplier γ = 1 − f:

```
out(q) = γ · q · R_out / (R_in + γ · q)
```

Uniswap V2's `getAmountOut` is exactly this with γ = 997/1000. Uniswap V3 behaves the same **within a single tick range**, with virtual reserves derived from liquidity L and √P:

```
x_virtual = L / √P        y_virtual = L · √P
```

so an in-range V3 swap is a constant-product swap on virtual reserves. The moment the trade crosses an initialized tick, L changes and the function becomes piecewise — more on that in §2.3.

### 2.2 Optimal input for two pools (closed form)

Buy on pool A (reserves a of X, b of Y, fee γ₁), sell on pool B (reserves c of Y, d of X, fee γ₂). Composing the two swaps gives the round-trip output:

```
F(q) = γ₁γ₂·b·d·q / (a·c + γ₁(c + γ₂·b)·q)
```

Maximise Π(q) = F(q) − q. Setting F′(q) = 1 (not F′ = 0 — marginal output must equal marginal input) gives:

```
q* = ( √(γ₁γ₂·a·b·c·d) − a·c ) / ( γ₁(c + γ₂·b) )
```

with a positive opportunity iff `γ₁γ₂·b·d > a·c` (the marginal price condition). Equal fees reduce to `q* = (γ√(abcd) − ac) / (γ(c + γb))`.

I verified the closed form against a numerical maximisation of Π(q) on three pool configurations — the analytic optimum and the grid search agree to the cent.

With a flash-loan premium λ, maximise F(q) − (1+λ)q:

```
q*_loan = ( √(γ₁γ₂·a·b·c·d/(1+λ)) − a·c ) / ( γ₁(c + γ₂·b) )
```

Fixed costs (gas) do not move the continuous optimum, but they move the **decision to trade at all** — see §2.6.

### 2.3 Uniswap V3 and tick crossings

There is no single reserve pair for a V3 pool. Liquidity is concentrated in ranges; as price moves the pool crosses initialized ticks and active liquidity changes. Within an interval with liquidity L, moving √P from s to t:

```
|Δx| = L·|1/t − 1/s|          |Δy| = L·|t − s|
```

The swap algorithm walks tick to tick, applies the fee, updates L by the tick's liquidity delta, and continues. **The closed form breaks the first time a tick is crossed**, because the virtual reserves change discontinuously.

Practical method:

1. Build the route as a scalar function F(q) using exact pool math (fee, rounding, price limit, tick updates).
2. Split the search at the inputs that correspond to tick boundaries (each segment is smooth and concave).
3. Optimise per segment — bisection on marginal profit `dF/dq − 1`, or golden-section on Π — then take the best.
4. Re-quote at execution state. Any quote older than one block is a suggestion.

For a first build, restricting to in-range swaps (trade size small relative to the price range) lets you use the closed form as a *seed*, then refine with an exact simulation. That is what I would ship: closed form to find the neighbourhood, exact simulator to pick the integer size.

### 2.4 Profit scales with liquidity × dislocation² — the number that sets expectations

Two equal pools, zero fees, dislocation δ:

```
Π_max ≈ L · δ² / 4
```

The intuition: you cannot trade the whole notional at the good price; each unit you push moves the price against you, and you stop when the marginal edge reaches zero. You capture the triangle between the two curves, not the gap times the size.

Measured with the real formulas (γ = 0.9995 buy leg, 0.9999 sell leg — i.e. 5bp + 1bp), WETH at $2,340:

| Pool depth | 10 bp | 25 bp | 50 bp | 100 bp | 300 bp |
|---|---|---|---|---|---|
| $2M | $0.04 | $0.90 | $4.83 | $21.97 | $212.83 |
| $20M | $0.40 | $9.01 | $48.26 | $219.71 | $2,128.30 |
| Optimal size at $20M | $2.0k | $9.5k | $22.0k | $46.9k | $145.9k |

Read that table twice. **A 25bp dislocation — a large move by AMM standards — is worth nine dollars on a $20M pool.** This is why:
- professionals need thousands of pools and near-zero latency to make a living;
- the fee floor (6bp for the cheapest realistic pair) knocks out most of the small-dislocation column entirely;
- the long-tail is where the money is: 900bp on a $250k pool returned **$175** on a $4.7k trade in the same model;
- a "0.3% per hop" design needs a 35bp hole before it earns a cent, and on majors those holes exist for ~200ms a few times a week.

### 2.5 The full cost stack

```
net = Π(q*) − gas_exec − l1_data_fee − flash_premium − priority_fee(tip) − builder_payment
```

**L2 execution gas.** Planning envelope for a flash-loan + 2-swap atomic arb: **200k–400k gas**, 300k as a working number.

| Chain | Gas price (Oct 2026) | 300k gas | With L1 data fee (planning) |
|---|---|---|---|
| Base | ~0.006 gwei (0.005 base + ~0.001 tip) | ~$0.004 | **$0.01–$0.05** |
| Arbitrum One | ~0.020 gwei | ~$0.014 | **$0.02–$0.10** |

**L1 data fee is not optional and not refundable.** Both chains charge it on included transactions, revert or not:

- **OP Stack / Base:** `GasPriceOracle` at `0x420000000000000000000000000000000000000F`, call `getL1Fee(serializedTx)`. Post-Ecotone it mixes L1 base fee and blob base fee via scalars; post-Fjord it estimates compressed size with a FastLZ model. **Query the oracle; do not hand-roll the formula** — the scalars change.
- **Arbitrum Nitro:** the fee is folded into the gas price. Use `NodeInterface` at `0x00000000000000000000000000000000000000C8`, `gasEstimateComponents(...)` / `gasEstimateL1Component(...)`. Nitro sets the L1 calldata payment aside **before** EVM execution, so a revert does not give it back.

Blob economics moved in Dec 2025: Fusaka's EIP-7918 makes the blob reserve price track L1 execution base fee (`8192/131072 = 1/16` of it), so blob fees are no longer pinned at 1 wei. Planning blob base fee at the time of writing: ~0.006 gwei. It can spike, and when it does, revert costs rise with it — which is why the revert budget must be dynamic, not a constant.

**Fee arithmetic per route** (the thing that kills most "arbitrage bot" designs):

| Route | Round-trip fee | Max profitable dislocation observed |
|---|---|---|
| 1bp → 1bp | 2 bp | 2–10 bp |
| 1bp → 5bp | 6 bp | 6–25 bp |
| 5bp → 30bp (Sushi) | 35 bp | 35–60 bp, rare |
| 30bp → 100bp | 130 bp | 130–900 bp, thin pools |
| 100bp → 100bp | 200 bp | 200–2,000 bp, thin pools + events |

**Priority fee.** On Arbitrum this is now the ordering mechanism, so the tip is an auction bid, not a convenience. On Base, tips matter but arrival order does too. The bid should be a **fraction of expected profit** (10–40%), capped by an absolute ceiling, and it must be part of the EV calculation — bidding the full profit is how searchers work for free.

### 2.6 Breakeven size and the go/no-go test

Ignoring slippage, net = q·edge_bp/10⁴ − gas, so the minimum size that clears gas:

```
q_min = gas / (edge_bp / 10⁴)
```

At $0.03 gas: **$150 at 2bp, $60 at 5bp, $30 at 10bp**. Below those sizes the trade is a donation to the sequencer. Above them, the real limit is `q*` from §2.2 — and `q*` is usually *smaller than people guess*, because slippage closes the gap fast.

The decision rule for the engine is therefore three tests, in order:

1. **Existence:** does an opportunity exist after fees? (`γ₁γ₂bd > ac`)
2. **Size:** is `q*` above `q_min`, and is Π(q*) above the gas + tip + flash premium?
3. **Confidence:** is the simulated profit at execution state above the on-chain `minProfit` floor?

Only then sign and submit. Anything else is a paid lesson.

### 2.7 Worked examples (real formulas, WETH $2,340)

**A. Majors, 100bp dislocation, $20M pools, 5bp + 1bp fees**
q* = $46.9k, gross $219.71, gas $0.03, net **$219.68**. Sounds great — but a 100bp dislocation on WETH/USDC is a once-a-month event and it is gone in one block.

**B. Majors, 25bp dislocation, same pools**
q* = $9.5k, gross $9.01, net **$8.98**. The bread-and-butter case. You need dozens of these a day to matter, and you are racing everyone.

**C. Uni V3 5bp vs Sushi 30bp, 45bp dislocation, $8M pools**
q* = $1,975, gross **$0.97**, net $0.91. This is the route as specified, at a dislocation *above* its fee floor. It is not a business.

**D. Long-tail, 900bp dislocation, $250k pool, 1% + 30bp fees**
q* = $4,709, gross **$175.35**, net $175.32. One event. This is the business.

That contrast is the entire argument for re-scoping the strategy.

---

## 3. Execution infrastructure

### 3.1 On-chain executor (the only component that touches money)

Minimal, stateless, no admin keys, no upgradeability:

```
execute(Route[] routes, uint256 minProfit, uint256 deadline)
  ├─ flash-borrow input asset (Balancer V2 or Morpho, 0%)
  ├─ for each hop: approve exact amount → swap (router or direct pool call)
  ├─ verify: balanceOf(output) ≥ borrowed + minProfit
  └─ repay; else revert("no profit")
```

Design rules:

- **All-or-nothing.** The profit check is the last statement; if it fails, the whole transaction reverts and the only cost is gas. This is what makes the strategy safe — you never hold a position, never get liquidated, never wake up long a token that dumped.
- **`minProfit` is set off-chain from the simulation**, with a small buffer for rounding and state drift. Too tight = constant reverts; too loose = you can execute a loser when the pool moved between quote and inclusion.
- **Allowlist assets and routers.** Fee-on-transfer tokens, rebasing tokens, ERC-777 hooks, and tokens with transfer taxes will break balance accounting and can drain the executor.
- **No owner functions.** A contract with a `withdraw()` is a contract someone will drain. Fund it with gas, not with capital; profits accumulate to a separate address via the same check.
- **Use direct pool calls, not the universal router,** for V2-style and simple V3 exact-input swaps: fewer external calls, less gas, fewer surprises. Use `SwapRouter02` only where V3 math is genuinely needed.

### 3.2 Off-chain stack

```
[chain WS/RPC] → pool state cache → opportunity graph → sizer → simulator → submitter → accounting
       │                │                    │              │           │
   Flashblocks     reserves / ticks     Bellman-Ford    closed form   private path
   or 125ms feed   (multicall)          on -ln price    + V3 segments  + tip bid
```

**State ingestion.** Subscribe to `Sync` (V2) and `Swap`/`Mint`/`Burn` (V3) logs for every watched pool; on Base prefer a Flashblocks-aware provider so you see `pending` state ~200ms before the block closes. Maintain reserves and tick data incrementally — do not re-read pools per opportunity.

**Opportunity discovery.** Build a graph of tokens and pools, weight edges by `−ln(effective price after fee)`, and look for negative cycles (Bellman-Ford). For 2-hop spatial arb this reduces to a pairwise price comparison, which is faster: keep a per-token price vector and only evaluate pairs whose price just changed.

**Sizing.** Closed form (§2.2) for the seed; exact simulation for the final integer amount.

**Simulation.** `eth_call` against the pending state (Base Flashblocks) or latest block (Arbitrum), preferably `eth_simulateV1` or a local `revm` fork with state overrides. A simulation that is not at execution state is a guess. Budget 10–30ms.

**Submission.**

| Path | Latency | Privacy | Cost | Use |
|---|---|---|---|---|
| Public RPC | 30–150 ms | none | free/cheap | Reads, simulation |
| Sequencer direct (Base / Arb) | lowest, no SLA | tx visible once sequenced | base + tip | Default submit path |
| Flashblocks-aware provider (Base) | ~200 ms preconfirm | sees pending state | subscription | Detection + submit |
| Alchemy MEV protection → Merkle/Blink | provider-dependent | private until inclusion | provider plan | Contested majors |
| bloXroute Base fast RPC | optimized | not hiding | tip ≥ 0.00008 ETH | Alternative submit |
| Arbitrum Fast Feed | early sight of *ordered* txs | read-only | ~$17/day | Detection only, after proving P&L |
| Flashbots Protect | — | — | — | **Not available on either chain** |

**Tip bidding.** Treat it as a second-price auction with a value equal to your expected profit: bid `clip(α · E[profit], floor, ceiling)` with α ≈ 0.1–0.3 on Arbitrum (where the tip *is* the ordering rule) and rely more on arrival time on Base. Log every bid against realized profit; the ratio is your rent to the sequencer, and if it exceeds ~40% you are not competitive.

**Latency budget** (target: under one 200ms Flashblock, ideally under 100ms):

| Stage | Budget |
|---|---|
| Log received → state updated | 5–20 ms |
| Opportunity detected → sized | 1–5 ms |
| Simulated at execution state | 10–30 ms |
| Signed + submitted | 5–15 ms |
| **Total** | **~25–70 ms** |

A Raspberry Pi on home broadband will not hit that. This needs a VPS in the sequencer's region with a persistent WS connection. That is a real architectural change from the current Ares deployment.

### 3.3 Why "MEV protection" is the wrong frame here

Protection products exist to stop *someone else* extracting value from *your* trade — sandwiching a user's swap. An arbitrage transaction is not a user trade; it is the extraction. There is no one to protect you from, and no builder to hide behind on a single-sequencer L2.

What you actually need is:

1. **Not telegraphing your intent** before inclusion (private submit path), so a competitor cannot copy your exact route and outbid you.
2. **Winning the ordering auction** when the opportunity is contested (tip).
3. **Cheap failure**, because you will lose most races.

That is why the design uses sequencer-direct submission plus a bid policy, and treats "Protect" as unavailable rather than as a missing integration.

---

## 4. Risk and failure modes

### 4.1 Risk register

| # | Risk | Mechanism | Impact | Mitigation | Detection |
|---|---|---|---|---|---|
| 1 | **Reverted tx** | Pool state moved between quote and inclusion; `minProfit` not met | Gas + L1 data fee, no position | Simulate at pending state; tight-but-buffered `minOut`; per-hour revert budget | Revert rate per route; $/day burned |
| 2 | **Lost race** | Competitor lands first, opportunity closes | Wasted gas, no loss of capital | Tip policy; coverage over speed; skip known-contested routes | Compare your attempt with the on-chain winner |
| 3 | **Quote→inclusion drift** | Price moves in the 100–300ms gap | Execution at worse price, or revert | Atomic min-out; never rely on off-chain price for the check | Slippage vs simulated, per trade |
| 4 | **LVR / adverse selection** | You are the slow one; the fast one takes the good fills | Persistent small losses | Measure hit rate by route; drop routes where you are always second | Per-route win rate |
| 5 | **Smart contract bug** | Executor logic, approval handling, callback auth | Loss of gas tank; potentially stuck funds | Minimal stateless contract, no admin, allowlist, fork tests, external review before real size | Invariant tests; canary tx |
| 6 | **Malicious / weird token** | Transfer tax, rebasing, hooks, blacklist | Balance check passes/fails wrongly; funds stuck | Asset allowlist; simulate exact token behavior; skip unknown tokens | Simulation mismatch |
| 7 | **Flash loan failure** | Provider liquidity, premium change, callback mismatch | Revert; missed opportunity | Query premium on-chain; two providers with fallback | Revert reason decoding |
| 8 | **Sequencer outage / reorg** | Base/Arb downtime; Flashblock reorg (rare but real) | Missed trades; tx dropped | Treat preconfirm as signal not finality; no inventory so no naked exposure | Slot lag, missing blocks |
| 9 | **Operational** | Nonce gap, RPC rate limit, clock skew, disk full | Outage, stuck txs | Multiple RPCs, nonce manager, health checks, alerting | Heartbeat, pending-tx age |
| 10 | **Key compromise** | Hot wallet on a server | Gas tank drained | Dedicated key, small balance, sweep profits, no key in repo/env on shared hosts | Balance monitor |
| 11 | **Parameter drift** | Aave premium, Base operator fee, blob fee spike, fee-tier change | EV model wrong | Read parameters on-chain at runtime, never hardcode | Daily parameter snapshot |
| 12 | **Competitive erosion** | More searchers, tighter spreads | Margins → 0 | Diversify coverage; move to less-contested pairs; accept lower frequency | Monthly net per route |

### 4.2 The three that actually kill small operators

**Gas-wasting reverts.** Cheap per event ($0.01–$0.05), fatal at volume. At 10 attempts/second you are burning $8–$40/hour for nothing. Cap attempts per block per route, require simulated EV > 3× gas, and alert when the revert rate crosses 70%.

**Being the slowest searcher on the best routes.** The failure is silent: you see the opportunity, you submit, you lose, you pay. Instrument the *outcome* — for every attempt, find the winning tx in the same block and record who won and by how much. If you are never winning on a route, delete the route. Do not tune blindly.

**Treating preconfirmations as finality.** On Base a Flashblock can reorg; on Arbitrum Fast Feed explicitly guarantees neither inclusion nor replay. Since the executor is atomic, a reorg costs gas and a missed trade, never a naked position — but the accounting must not count reorged profits as realized.

### 4.3 What is *not* a risk here (and why the design is safer than it looks)

- **No inventory risk.** Flash loans mean you never hold the asset across blocks.
- **No liquidation risk.** No borrow position persists.
- **No funding/oracle risk.** Nothing is held long enough to be marked.
- **No counterparty risk beyond the DEX contracts and the flash provider**, both of which are the same primitives everyone else uses.

The strategy's risk profile is: *many small, bounded losses and operational fragility*, not *one large blowup* — provided the executor stays minimal and the key stays poor.

---

## 5. Reference architecture

### 5.1 Components

| Component | Responsibility | Tech | Notes |
|---|---|---|---|
| `executor` | Atomic flash-loan + swaps + profit check | Solidity 0.8.x, Foundry | Stateless, no admin, allowlist |
| `watcher` | Pool discovery + log ingestion + state cache | Rust (`alloy`) or TS (`viem`) | One process per chain |
| `graph` | Token/pool graph, price vector, negative cycles | Rust | In-memory, rebuilt from cache |
| `sizer` | Closed form + V3 segment search | Rust | Shared library with the simulator |
| `simulator` | Exact route execution at pending state | `revm` (local) or `eth_simulateV1` | Must match on-chain math bit-for-bit |
| `submitter` | Sign, tip, submit, track nonce/inclusion | Rust + `alloy` | Multiple RPCs, per-route budget |
| `accountant` | Realized P&L, gas, tips, revert rate, per-route stats | Python + SQLite/Postgres | Feeds the dashboard |
| `dashboard` | Ops view | Existing Flask app | Read-only telemetry panel |

**Language choice.** The existing Ares desk is Python, and Python is fine for the accountant, the research replays, and the dashboard. It is **not** fine for the hot path: interpreter overhead and GC pauses are a meaningful fraction of a 100ms budget. Keep research in Python, write the executor service in Rust (or TypeScript if the team is stronger there), and let them share a JSON schema for routes and results.

### 5.2 Data flow

```
1. watcher sees Swap/Sync on a watched pool (or a Flashblock pending update)
2. state cache updates reserves/ticks for that pool
3. graph re-prices affected token pairs; emits candidates
4. sizer computes q* and expected profit net of fees
5. filter: profit > gas + tip + flash premium + margin
6. simulator runs the exact route at pending state
7. submitter signs, bids tip, submits to sequencer/private path
8. inclusion → accountant records realized P&L; revert → records burn
9. every N minutes: per-route stats → disable routes below threshold
```

### 5.3 Staged rollout (with go/no-go gates)

| Phase | What runs | Capital | Gate to proceed |
|---|---|---|---|
| **0. Replay** (1–2 wks) | Historical logs replayed through sizer + simulator; count opportunities and $/day | $0 | ≥ $50/day of *simulated* gross on ≥ 3 routes you can actually win |
| **1. Shadow** (1–2 wks) | Live detection + simulation, **no submission**; compare against actual on-chain arbs in the same block | $0 | ≥ 20% of the opportunities you found were *not* taken by anyone (i.e. you would have won) |
| **2. Canary** (2–4 wks) | Live on one chain, top 20 pools, $200 gas tank, hard caps | $200–500 | 30-day net > 0 after gas and tips |
| **3. Scale** | Add pools/tokens, add second chain, consider Fast Feed | $500–1,000 gas | Net/attempt improving month over month |

**Kill criteria** (write them down before starting): if after Phase 2 the 30-day net is negative and the per-route win rate is below 20%, stop. Do not average down into a latency race you cannot win.

**Hard safety caps for Phase 2:** max 3 attempts/block, max $20/day gas+tip, max 1 open tx at a time, auto-pause if revert rate > 80% over 200 attempts, auto-pause if the gas tank drops below 3 days of budget.

---

## 6. What this realistically earns

Working from the model and the published measurements:

- Published MAV (maximal arbitrage value) on Arbitrum/Base/Optimism WETH/USDC is **0.03–0.05% of volume** — a few million dollars a year across *all* searchers on those chains. Splitting that among the incumbents is the honest frame.
- In a large CEX–DEX study, **~90% of trades had gross returns below 20bp**, and net margins were 20–70% of gross depending on builder integration (Wu et al., 2025). That is the shape of the distribution: small, frequent, mostly competed away.
- For a solo operator with good coverage but no private orderflow, a defensible target is **$300–$1,500/month gross** after the first two months, of which infra costs ($50–$200 VPS, $0–$300 RPC, optional $510/mo Fast Feed) and gas ($30–$150) take a meaningful bite. **Net near zero in month one is the expected outcome, not a failure.**

Anyone promising more than that from a standing start is selling something. The edge here is engineering discipline: cheaper execution, broader coverage, and ruthlessly deleting routes that lose.

---

## 7. Relationship to the current Ares build

This is a **different system** from the running desk, not a strategy swap:

| | Current Ares desk | MEV arb engine |
|---|---|---|
| Venue | Alpaca (CEX, off-chain) | On-chain DEX |
| Latency need | minutes/hours | tens of milliseconds |
| Risk | position/drawdown | gas + reverts, bounded |
| Capital | notional | gas only (flash loans) |
| Host | Raspberry Pi, home network | VPS in sequencer region |
| Code | Python/Flask, SQLite | Rust service + Solidity executor |
| "Paper mode" | yes (Alpaca paper) | **no** — gas is real ETH |

Recommendation: **keep the existing desk running untouched**, put the arb engine in its own repo/service, and use the existing Flask dashboard purely as a read-only telemetry panel (routes, attempts, reverts, net). Do not try to bolt an on-chain executor onto the Pi — the host and the language are both wrong for it, and a shared process would put the running desk at risk.

The one thing worth reusing: the **accountant and the honesty discipline**. Track every attempt, every revert, every tip, and report net — not gross. The failure mode of every MEV bot is a dashboard that shows gross profit while the wallet drains.

---

## 8. Decisions needed before any code

1. **Chain.** Base (cheapest gas, 200ms Flashblocks, arrival-order matters) or Arbitrum (250ms, priority-fee auction with a 2×125ms round, Fast Feed available). I would start on **Base** — lower cost of failure, better preconfirmation data, and the tip auction is less winner-take-all than a pure PGA.
2. **Coverage strategy.** Majors-only (proves the pipeline, low P&L) or majors + long-tail (real P&L, more token risk). I would build the pipeline on majors and earn on long-tail.
3. **Capital for gas.** $200–$500 on Base is enough for Phase 2. This is real money and it can be lost to gas — that needs to be acceptable before starting.
4. **Hosting.** A VPS in the sequencer's region, not the Pi. Roughly $20–$80/month.
5. **Whether to pay for Fast Feed** (~$17/day, Arbitrum only). Not until Phase 3, and only if Phase 2 shows you are losing races you would otherwise win.

---

## Sources

- Arbitrum, *Sequencer transaction flow* — https://docs.arbitrum.io/how-arbitrum-works/deep-dives/sequencer-transaction-flow
- Arbitrum, *Priority gas auction (PGA)* — https://docs.arbitrum.io/how-arbitrum-works/priority-gas-auction/pga
- Arbitrum, *Fast Feed* and *How to buy access* — https://docs.arbitrum.io/how-arbitrum-works/priority-gas-auction/fast-feed · https://docs.arbitrum.io/how-arbitrum-works/priority-gas-auction/use-fast-feed
- Arbitrum Foundation, *Builder's Block #027* (PGA replaces Timeboost, Oct 2026) — https://blog.arbitrum.foundation/builders-block-027-learn-about-arbitrums-new-transaction-ordering-ideas-for-builders-at-founder-house-singapore/
- Arbitrum, *Gas and fees* / *NodeInterface reference* — https://docs.arbitrum.io/how-arbitrum-works/deep-dives/gas-and-fees · https://docs.arbitrum.io/arbitrum-essentials/nodeinterface/reference
- Base, *Flashblocks deep dive* — https://www.base.org/blog/flashblocks-deep-dive
- Base, *Flashblocks specification* / *API overview* — https://docs.base.org/specifications/flashblocks · https://docs.base.org/base-chain/api-reference/flashblocks-api/flashblocks-api-overview
- Base, *Transaction ordering* — https://docs.base.org/specifications/transactions/transaction-ordering
- Base, *Network fees* — https://docs.base.org/specifications/transactions/network-fees
- OP Stack, *Predeploys* and *Transaction fees* — https://specs.optimism.io/protocol/predeploys.html · https://docs.optimism.io/op-stack/transactions/fees
- EIP-4844 — https://eips.ethereum.org/EIPS/eip-4844 · EIP-7918 — https://eips.ethereum.org/EIPS/eip-7918
- Flashbots, *Protect quick start* (Ethereum mainnet only) — https://docs.flashbots.net/flashbots-protect/quick-start
- Alchemy, *MEV protection* (Arbitrum + Base via Merkle/Blink) — https://www.alchemy.com/docs/reference/mev-protection
- bloXroute, *Fast RPC* / *Protect RPC* — https://docs.bloxroute.com/core-solutions/rpc-service/fast-rpc · https://docs.bloxroute.com/core-solutions/rpc-service/protect-rpc
- Balancer V2, *Flash loans* — https://docs-v2.balancer.fi/reference/contracts/flash-loans.html
- Aave V3, *Flash loans* — https://aave.com/docs/aave-v3/guides/flash-loans
- Morpho, *Flash loans* — https://docs.morpho.org/learn/concepts/flashloans/
- Uniswap V3, `SwapMath.sol` — https://github.com/Uniswap/v3-core/blob/main/contracts/libraries/SwapMath.sol
- Uniswap V2, `UniswapV2Library.sol` — https://github.com/Uniswap/v2-periphery/blob/master/contracts/libraries/UniswapV2Library.sol
- Milionis, Moallemi, Roughgarden, Zhang, *Automated Market Making and Loss-Versus-Rebalancing* — https://arxiv.org/abs/2208.06046
- Milionis, Moallemi, Roughgarden, *Automated Market Making and Arbitrage Profits in the Presence of Fees* — https://arxiv.org/abs/2305.14604
- Fritsch & Canidio, *Measuring Arbitrage Losses and Profitability of AMM Liquidity* — https://arxiv.org/html/2404.05803v2
- Gogol et al., *Cross-Rollup MEV* — https://arxiv.org/html/2406.02172v2
- Wu et al., *Measuring CEX-DEX Extracted Value and Searcher Profitability* — https://arxiv.org/html/2507.13023v2
- Flashbots, *simple-blind-arbitrage* (reference executor pattern) — https://github.com/flashbots/simple-blind-arbitrage
