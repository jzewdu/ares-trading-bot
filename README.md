# Ares — trend core on eight majors

A small Python service that owns **majors on a paper Alpaca account** while their
slow trend is intact, sells when a daily close breaks far enough to matter, and
waits in cash until the market earns a new entry. It runs four positions at a
time out of a book of eight, sized so a catastrophe stop costs 2% of equity.

Paper only. Every entry and exit is a real order on the Alpaca paper account.

## What changed, and why

The desk used to read its 200-bar trend line on **4-hour** candles. Two things
were wrong with that, and both were measurable:

1. **200 bars of 4 hours is a 33-day EMA**, not a 200-day one. On an instrument
   that charges 25bp per side, a 33-day trend hands most of the edge to the
   exchange. The same code on daily bars crosses twice in six years on BTC
   instead of 77 times.
2. **The re-entry gate only fired after a brake exit.** An exit recorded for any
   other reason carried no anchor, so the book bought straight back. The live
   account shows the cost:

| Time (UTC) | Action | Price | Outcome |
| --- | --- | --- | --- |
| Oct 28 01:58 | buy 0.318 BTC | 80,885 | — |
| Oct 28 23:04 | sell (handover) | 81,124 | −$37.53 |
| **Oct 28 23:14** | **re-buy 0.311 BTC** | **81,290** | 10 min after its own exit |
| Oct 29 03:03 | sell (brake) | 79,840 | ≈ −$580 |
| **Oct 29 11:05** | **re-buy 0.295 BTC** | **80,150** | 0.39% over its own brake sell |
| Oct 29 23:04 | sell | 78,635 | ≈ −$564 |

Roughly **$1.1k** went to buying back a dip the desk had just sold. The gate now
applies to **any** exit, and the anchor is written to the state store on the way
out, so a restart on a fresh database cannot forget it.

## The rule

Measured on **closed daily candles**, using only information that was complete
when the desk acted:

| State | Test on the last closed candle | What the desk does |
| --- | --- | --- |
| **Long** | close > 200-day EMA − 3 ATR | Holds the core long |
| **Brake** | close < 200-day EMA − 3 ATR | Sells and waits |
| **Re-arm** | after any exit | Needs a close above the brake + 1.5 ATR before paying another round trip |

The band between the line and the brake is deliberate hysteresis: a dip below
the line is normal, a break through the band is not. There is no trailing stop
and no target — the brake is the exit. On top of that sits a catastrophe stop at
**8 ATR** below the fill, checked every five minutes.

Eight majors are scanned (`BTC, ETH, SOL, LINK, AVAX, DOGE, LTC, BCH`) and the
four highest-scoring setups are held. One coin was not enough: the slow rule
fires a handful of times a year per name, so a single instrument leaves the desk
idle for months.

## Risk

- **2% of equity** per position against an 8-ATR stop; four positions, **50% of
  equity** deployed at most.
- **One** position per coin, a 60-minute cooldown after a close.
- Entries pause for the rest of the UTC day after a 3% equity loss. Exits never
  pause.
- A spread above **0.15%** blocks an entry. On the day this shipped that alone
  refused LINK, LTC, AVAX and BCH, whose quotes were 0.23%–0.61% wide.
- A quote older than 15 minutes cannot be traded against, and a signal more than
  2 ATR stale is skipped.
- The desk sells **only the quantity its own ledger owns**. If another process
  or a manual trade adds to the same pair, the extra is never liquidated.
- The learner still runs: each close files net R into decayed buckets and can
  size a repeat of that kind of trade up to 1.4×, down to 0.35×, or refuse it
  after 8 trades of non-positive expectancy.

## What the replay says

`research/desk_replay.py` runs the shipping rule over real Alpaca daily bars — a
200-day line, a 3-ATR brake, a 1.5-ATR re-arm, an 8-ATR stop, 0.30% per side
including slippage, equal weight across the eight majors:

| Measure | Basket of 8, daily |
| --- | --- |
| Trades | 77 (≈15/yr) |
| Win rate | 23% |
| Average net R | **+0.32R** |
| Total return | **+87.4%** |
| Max drawdown | 73.8% |
| Buy and hold, same window | **−7.8%** |

A 23% win rate is the shape of the thing: many small losses, a few holds that
pay for all of them. The drawdown figure is the honest cost of an equal-weight,
100%-notional replay; the live desk caps deployment at 50% and sizes by risk, so
its realised drawdown should be roughly half of that — still uncomfortable, and
not something to discover after the fact.

**The single most important caveat.** Right now BTC is above its 200-day line but
below its 33-day one. Over 2021-2026 that state produced *no* long edge:

| State (BTC daily) | fwd 5d | fwd 20d | win |
| --- | --- | --- | --- |
| uptrend, above 33d | +0.75% | **+2.90%** | 53% |
| **uptrend, below 33d (now)** | **−0.15%** | **+0.26%** | **47%** |
| downtrend, below 33d | +0.23% | +1.16% | 55% |

So the desk is long in the one state where the next month has historically been
a coin flip. That is a property of the rule, not a bug, and it is why the
position size is 20% of equity and not 100%.

## What is real

Every buy and sell is a market order on Alpaca's paper account, submitted and
polled to a terminal fill. Positions are sized from the broker's own quantity,
and every cycle compares the ledger against `GET /v2/positions`. Equity is
Alpaca's equity; cash is Alpaca's cash; P&L is net of the fees actually charged.

The first live book under this rule, 2026-10-30 01:48 UTC:

| Coin | Notional | Entry | Stop | Risk |
| --- | --- | --- | --- | --- |
| BTC | $8,355 | 76,662.81 | 59,384.70 | $1,883 |
| ETH | $5,786 | 2,193.60 | 1,480.75 | $1,880 |
| SOL | $4,958 | 93.67 | 57.9639 | $1,880 |

Alpaca cannot short crypto, so the desk is **long-only**. There is no leverage,
no live-money path, and the client refuses to start unless `ALPACA_PAPER=true`.

## Files

- `btc_trend.py` — the rule: trend line, ATR brake, re-arm logic, signal scores.
- `regime.py` — bar aggregation and the indicator series both modules share.
- `learning.py` — decayed bucket statistics, the sizing multiplier, avoid gate.
- `engine.py` — the loop: analyse, manage exits, size and submit entries,
  reconcile against the broker, record marks.
- `state.py` — SQLite: trades, equity marks, decisions, events, learned buckets,
  and the re-arm anchors.
- `alpaca_client.py` — paper Alpaca REST client, paged bars, fill polling,
  truncating quantity formatting.
- `app.py`, `templates/`, `static/` — authenticated dashboard.
- `test_engine.py` — trend/brake states, signal geometry, learner avoidance and
  decay, broker-backed entry, stop exit, fee accounting, spread gate, the re-arm
  gate, the foreign-position guard and the retrying loop.
- `research/basket_bt.py` — the rule on a basket of majors vs buy and hold.
- `research/twoside_bt.py` — one-sided vs two-sided, spot vs IBIT, half-samples.
- `research/desk_replay.py` — writes `btc_summary.json`, the dashboard's panel.
- `scripts/run_ares.cmd` — restart-on-exit launcher with a duplicate guard.

## Run it

```bash
cp .env.example .env      # paper keys + dashboard credentials, then chmod 600
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python test_engine.py
python app.py             # dashboard on 127.0.0.1:8090
```

On Windows the desk is started by `scripts\run_ares.cmd`, which restarts it if
it exits and refuses to start a second copy. A shortcut lives in the user's
Startup folder so it comes back at logon; logs land in `logs\ares.log`.

The dashboard binds to localhost. Reach it through an SSH tunnel; do not expose
it without TLS. `ARES_STATE_PATH` points at the SQLite file.

On the Pi the service is `ares-bot.service` (gunicorn, state in
`/var/lib/ares/ares_state.sqlite3`). Without root, reload code with
`kill -HUP $(systemctl show ares-bot -p MainPID --value)`; gunicorn re-forks the
worker, which re-imports the app and starts a fresh engine thread.

To regenerate the replay panel:

```bash
.venv/bin/python research/desk_replay.py
```

## On-chain arbitrage (separate from the desk above)

A second line of work on Base: measure a cross-pool dislocation, then capture it
atomically or not at all.

- [docs/mev-arb-research.md](docs/mev-arb-research.md) — strategy, sizing math,
  execution paths, risk register, and why the Uniswap-vs-Sushi route as
  originally specified is negative-EV.
- [docs/mev-phase0.md](docs/mev-phase0.md) — the shadow scanner and what it
  measured on Base mainnet.
- [docs/executor.md](docs/executor.md) — the executor contract, its tests
  (including real-state forks), and the measured result of the whole loop.
- [contracts/src/AresArb.sol](contracts/src/AresArb.sol) — Balancer flash loan →
  two pool swaps → repay and beat the floor, or revert.

```bash
python -m mev.scanner --once --top 120 --pages 10    # find edges
python -m mev.analyze mev/out/opportunities.jsonl     # dedupe them
python -m mev.executor --demo                        # whole loop on a Base fork
python -m mev.executor --dry-run                     # best live route, simulated
```

## Known limits

- **In-sample replay.** One window, one parameter set. No walk-forward yet.
- **The current state has no measured edge.** See the table above; the desk is
  long because the rule says so, not because this month looks good.
- **Fees dominate short horizons.** Anything that trades more than a few times a
  month on this account has to beat 0.5% per round trip before it earns.
- **Long-only crypto.** A falling market means cash, not profit. Alpaca cannot
  short spot crypto; `IBIT` was tested as a shortable vehicle and a two-sided
  daily rule on it returned **−15.3%**, so it was not adopted.
- **Gap risk.** The stop is checked every five minutes and sells at market, so a
  violent move can fill well through it.
- **Wide spreads are invisible until you look.** Four of the eight majors are
  routinely above the 0.15% gate, which quietly narrows the book to BTC/ETH/SOL.
- **One host at a time.** A second copy of this service on the same Alpaca
  account will disagree about quantities; the ledger only sells what it owns,
  but two engines still share one equity figure.
- **Paper only.** Nothing here has been validated with real money, and the
  dashboard is protected by a single HTTP Basic credential.
