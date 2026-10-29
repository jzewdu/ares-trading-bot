# The executor: from a measured edge to an atomic trade

The scanner proves a dislocation exists. This is the part that captures it, and
it is built around one rule that has not been broken once during testing:

> **A route that cannot be simulated profitably is never submitted.**

Everything below was run on this machine against Base mainnet state. Where a
number is missing, it is because it has not been measured, not because it was
rounded in our favour.

## What exists

| Piece | What it does |
| --- | --- |
| `contracts/src/AresArb.sol` | Atomic executor: Balancer flash loan → hop → hop → repay + profit, or revert |
| `contracts/test/AresArb.t.sol` | 17 unit tests with mock pool/vault/tokens, including 512 fuzz cases |
| `contracts/test/AresArbFork.t.sol` | 4 tests against **real** Base state: real Vault, real Aerodrome Slipstream and Uniswap V3 pools |
| `mev/venues.py` | Verified venue registry and the executor ABI |
| `mev/executor.py` | Route builder, ABI encoder, simulator, submitter, fork harness |
| `mev/test_executor.py` | 33 offline checks: encoding round-trips, refusals, floors |

## Why pools and not routers

Every venue the scanner measured speaks the Uniswap V3 **pool** interface.
Swapping the pool directly removes a router address per venue, removes token
approvals to third-party contracts, and makes each hop's minimum output
explicit. Verified on mainnet with `eth_call` before being written down:

| Contract | Address | Evidence |
| --- | --- | --- |
| Balancer V2 Vault | `0xBA12…F2C8` | `getProtocolFeesCollector()` → `0xce88…9F9F`; flash fee 0 |
| Aerodrome Slipstream pool | `0xb2cc…DC59` | `token0()` = WETH, `slot0()`/`liquidity()` live |
| Uniswap V3 pool | `0x6c56…1372` | same interface, 30bp fee |
| Aerodrome Slipstream Router | `0xBE6D…18a5` | `factory()` → `0x5e7B…809A` (not used by the executor) |

Balancer rather than Aave is arithmetic, not taste: Aave charges **5bp on the
borrowed notional**, and the gaps the scanner sees are 3–20bp. Balancer's fee is
zero, so the whole edge survives to the profit check.

## The contract's invariants

`execute(loanToken, loanAmount, hops[], minProfit, profitTo)`:

1. `hops[0].tokenIn == loanToken` and `hops[0].amountIn == loanAmount`, so the
   borrowed asset is the one being spent.
2. Every hop chains (`hops[i].tokenIn == hops[i-1].tokenOut`).
3. The last hop returns `loanToken`, so the loan is repayable by construction.
4. Intermediate hops use `spendBalance`, i.e. they spend exactly what the
   previous hop delivered. No dust is stranded and no amount is short by a few
   wei when the next block prices the pool differently.
5. Each hop reverts if it pays less than `minOut` (the quote minus the slippage
   tolerance), so a sandwich or a stale quote fails instead of filling badly.
6. After the hops: `balance >= loanAmount + fee + minProfit`. **This single line
   is the strategy's entire risk control.** Nothing after it runs if the route
   did not make money, and the whole transaction reverts.
7. `receiveFlashLoan` is callable only by the Vault; `uniswapV3SwapCallback` only
   by the pool currently being swapped. Both are tested.

## What was measured

**Contract tests — 21 pass, 0 fail.**

```
forge test
17 unit tests (mock vault, pools, tokens; 512 fuzz runs)
 4 fork tests (real Base state)
```

The fork tests prove the things unit tests cannot: the real Vault lends, real
Aerodrome Slipstream and Uniswap V3 pools call `uniswapV3SwapCallback`, the
principal is repaid to the Vault, the profit lands in the treasury, and a route
that loses money reverts with `Unprofitable`.

**The loop, end to end, on a Base fork:**

```
python -m mev.executor --demo --inject-bps 80
  edge 100.2bp   size 29,905.49 USDC   simulation profit 181.29 USDC
  EXECUTED  status 1  gas 565,915   treasury received 182.19 USDC
```

The 100bp gap was *injected into the fork* by the harness (`--inject-bps`), so
this is a proof of the capture mechanism, not a market result. The pools, the
Vault, the tokens and the contract are all real; only the dislocation is ours.

**The live market, measured rather than assumed.** Six WETH/USDC pools, 41
sampled Base blocks:

```
largest gap found                 32.8bp   (5.35bp pool vs 30bp pool)
combined fees on that route       35.35bp
net after fees                    -2.5bp
```

That is the market working: gaps do not exceed the fee to close them. On the
blocks sampled, the scanner found **no route worth executing**, and the executor
submitted nothing. That is the system behaving correctly, and it is the honest
answer to "why is it not trading".

## Running it

```bash
# Prove the whole path on a local fork (no key, no funds)
python -m mev.executor --demo

# Same path, with a known dislocation so the capture is visible
python -m mev.executor --demo --inject-bps 120

# Live: simulate the best route right now, submit nothing
python -m mev.executor --dry-run

# Live: submit (needs a funded key)
ARB_PRIVATE_KEY=... python -m mev.executor --deploy
ARB_PRIVATE_KEY=... python -m mev.executor --execute
```

`--execute` refuses to send unless the simulation already clears the floor
(`max(--min-profit-usd, gas)`), and the contract refuses again on chain. A
losing trade therefore requires the market to move between simulation and
inclusion, which costs gas, not capital.

Env: `ARB_PRIVATE_KEY` (owner key, never printed), `ARB_EXECUTOR` (deployed
address), `ARB_PROFIT_ADDRESS` (treasury, defaults to the owner), `BASE_RPC`.

## Honest limits

- **This is not deployed to mainnet.** Deploying needs a key with ETH for gas;
  we have neither. The contract compiles, passes 21 tests including real-state
  forks, and has executed real swaps on a fork — but no mainnet transaction has
  been sent by this repo.
- **Thin edges are fragile.** The scanner sizes with a constant-product model of
  concentrated liquidity, which is exact inside the active tick range and
  optimistic outside it. The simulation is the arbiter; the model is a hint. On
  WETH/cbBTC the model was optimistic enough that simulation refused every
  route, which is why the executor now tries a ladder of sizes.
- **The Vault's liquidity is a real cap.** On Base the Balancer Vault held
  ~74k USDC and ~23 cbBTC during testing. Routes larger than half of that are
  refused rather than attempted.
- **We are not the fastest.** Base blocks are 2s; this loop reads state, sizes,
  simulates and signs in well under that only when the RPC cooperates. A public
  RPC is not competitive with searchers wired into builders. Private order flow
  (Flashbots-style bundles on Base) is the next step for actually winning races.
- **The harness is not a strategy.** `--inject-bps` writes a pool's storage on a
  local fork. It proves the plumbing; it does not predict returns.
