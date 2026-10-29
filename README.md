# Ares — BTC trend core

A small Python service for a Raspberry Pi 3 Model A+ that owns **one instrument,
BTC/USD**, on a paper Alpaca account. It holds a core long while bitcoin's trend
is intact, sells when a 4-hour close breaks the trend far enough to matter, and
waits in cash until the market earns a new entry.

Paper only. Every entry and exit is a real order on the Alpaca paper account.

## Why this and not the gear desk

The previous build scanned twelve coins for trend/range setups. On real bars it
produced ~1.6 trades a week and, in this market, stood aside for days. So the
strategy layer was replaced with something measured rather than assumed. All of
these were replayed on real Alpaca bars with the desk's own costs — 0.25% per
side plus 0.05% slippage, 0.75%–2% risk sizing:

| Family (long-only, spot) | Result on the replay |
| --- | --- |
| 1h Donchian breakouts (24–72 bars, with/without trend filter) | negative after fees |
| 1h lower-band mean reversion (RSI 25–30) | negative, ~0.14 trades/day |
| 4h EMA trend with whipsaw exits (enter above the line, exit below) | negative — ~150 round trips paid 75% of the account in fees |
| 4h trend with hysteresis bands (many variants) | break-even at best, ~57% exposure |
| **Hold BTC, sell only on a real break** | **+8.18%, 6.35% max drawdown, 66% of the time in the market** |
| Buy and hold BTC, same window | +22.4%, larger drawdown, 100% exposure |

The conclusion was uncomfortable but clear: **at 0.25% per side, turnover is the
enemy.** Every rule that traded often lost to fees and whipsaw; the only thing
that made money was being long and not interfering. The desk is now that
observation with a risk brake attached.

## The rule

Measured on **closed 4-hour bars**, using only information that was complete
when the desk acted:

| State | Test on the last closed bar | What the desk does |
| --- | --- | --- |
| **Long** | close > 200-bar EMA − 2 ATR | Holds the core long |
| **Brake** | close < 200-bar EMA − 2 ATR | Sells and waits |

The band between the line and the brake is deliberate hysteresis: a dip below
the line is normal, a break through the band is not. Re-entry happens as soon as
the close is back above the brake. There is no trailing stop and no target —
the brake is the exit. On top of that sits a catastrophe stop at **8 ATR** below
the fill, checked every five minutes.

## Risk

- **2% of equity** is the risk budget; with an 8-ATR stop that puts roughly a
  quarter to a third of equity into BTC, capped at **50%** per position and 50%
  in total.
- **One** position, one instrument, a 60-minute cooldown after a close.
- Entries pause for the rest of the UTC day after a 3% equity loss. Exits never
  pause.
- A spot spread above **0.15%** blocks an entry, a quote older than five minutes
  cannot be traded against, and a signal more than 2 ATR stale is skipped.
- The learner still runs: each close files net R into decayed buckets and can
  size a repeat of that kind of trade up to 1.4×, down to 0.35×, or refuse it
  after 8 trades of non-positive expectancy.

## What the replay says

`research/btc_bt.py` runs the exact live rule over real Alpaca 4h BTC bars,
charging both fees and slippage. Two windows, because one is not evidence:

| Measure | 240 days | 900 days |
| --- | --- | --- |
| Trades | **4** | **21** |
| Win rate | **50%** | **24%** |
| Average net R | **+1.13R** | **+0.24R** |
| Return on equity | **+9.06%** | **+9.83%** |
| Max drawdown | **4.03%** | **9.20%** |
| Buy and hold, same window | +16.98% | +30.38% |
| Time in the market | 66% | 67% |

Three caveats, stated plainly:

1. **It underperforms simply holding.** +9.83% versus +30.38% over 900 days.
   The brake bought a 9.20% max drawdown instead of the 29% slide the second
   half of that sample contains. If you want maximum upside, holding wins; the
   desk is insurance, not alpha.
2. **Turnover is the thing that kills it.** Deleting the 3-ATR re-arm gate makes
   the same rule over the same 900 days return **−8.72%** across 123 round trips;
   at 1-ATR it still loses. The gate exists because the first live brake exit
   sold at $79,838 and the desk re-bought at $80,149 — $203 above its own exit
   trigger — and paid 50bp to stand in the same place.
3. **A green night is roughly a coin flip.** In an earlier replay, 33% of 12-hour
   windows ended higher (holding it was 50%), the median night was flat, and the
   worst was −1.4%. No configuration in any test made most nights green —
   bitcoin does not offer that.

## What is real

Every buy and sell is a market order on Alpaca's paper account, submitted and
polled to a terminal fill. Positions are sized from the broker's own quantity,
and every cycle compares the ledger against `GET /v2/positions`. Equity is
Alpaca's equity; cash is Alpaca's cash; P&L is net of the fees actually charged.
Positions worth less than a dollar are treated as dust.

Alpaca cannot short crypto, so the desk is **long-only**. There is no leverage,
no live-money path, and the client refuses to start unless `ALPACA_PAPER=true`.

## Files

- `btc_trend.py` — the rule: 200-bar trend line, ATR brake, signal and scores.
- `regime.py` — 4h bar aggregation and the indicator series both modules share.
- `learning.py` — decayed bucket statistics, the sizing multiplier, avoid gate.
- `engine.py` — the loop: analyse, manage exits, size and submit entries,
  reconcile against the broker, record marks.
- `state.py` — SQLite: trades, equity marks, decisions, events, learned buckets.
- `alpaca_client.py` — paper Alpaca REST client, paged bars, fill polling,
  truncating quantity formatting.
- `app.py`, `templates/`, `static/` — authenticated dashboard.
- `test_engine.py` — trend/brake states, signal geometry, learner avoidance and
  decay, broker-backed entry, stop exit, fee accounting, spread gate and the
  brake re-arm gate.
- `research/rearm_sweep.py` — the parameter sweep and half-sample check that
  chose the 3-ATR re-arm distance.
- `research/btc_bt.py` — the replay that writes `btc_summary.json`, which feeds
  the dashboard's replay panel.
- `gears.py`, `research/gears_bt.py` — the retired multi-coin gear desk, kept for
  reference; nothing imports `gears.py` any more.

## Run it

```bash
cp .env.example .env      # paper keys + dashboard credentials, then chmod 600
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python test_engine.py
gunicorn --workers 1 --threads 4 --bind 127.0.0.1:8080 app:app
```

The dashboard binds to localhost. Reach it through an SSH tunnel; do not expose
it without TLS. `ARES_STATE_PATH` points at the SQLite file.

On the Pi the service is `ares-bot.service` (gunicorn, state in
`/var/lib/ares/ares_state.sqlite3`). Without root, reload code with
`kill -HUP $(systemctl show ares-bot -p MainPID --value)`; gunicorn re-forks the
worker, which re-imports the app and starts a fresh engine thread.

To regenerate the replay panel:

```bash
.venv/bin/python research/btc_bt.py --days 240
```

## On-chain arbitrage (separate from the desk above)

A second line of work on Base: measure a cross-pool dislocation, then capture it
atomically or not at all.

- [docs/mev-arb-research.md](docs/mev-arb-research.md) — strategy, sizing math,
  execution paths, risk register, and why the Uniswap-vs-Sushi route as
  originally specified is negative-EV.
- [docs/mev-phase0.md](docs/mev-phase0.md) — the shadow scanner and what it
  measured on Base mainnet.
- [docs/executor.md](docs/executor.md) — the executor contract, its 21 tests
  (including real-state forks), and the measured result of the whole loop.
- [contracts/src/AresArb.sol](contracts/src/AresArb.sol) — Balancer flash loan →
  two pool swaps → repay and beat the floor, or revert.

```bash
python -m mev.scanner --once --top 120 --pages 10    # find edges
python -m mev.analyze mev/out/opportunities.jsonl     # dedupe them

python -m mev.executor --demo                        # whole loop on a Base fork
python -m mev.executor --demo --inject-bps 120       # ... with a known dislocation
python -m mev.executor --dry-run                     # best live route, simulated
python -m mev.executor --execute                     # submit (needs ARB_PRIVATE_KEY)
```

The scanner signs nothing. `--demo` runs the full path on a local fork with no
key and no funds; `--execute` refuses anything that does not simulate above the
profit floor.

## Known limits

- **In-sample replay.** One window, one parameter set. No walk-forward yet.
- **Fees dominate short horizons.** Anything that trades more than a few times a
  month on this account has to beat 0.5% per round trip before it earns.
- **Long-only.** A falling market means cash, not profit.
- **Gap risk.** The stop is checked every five minutes and sells at market, so a
  violent move can fill well through it.
- **Coarse Pi clock.** The daily-loss baseline resets on the UTC date; after a
  power cut, wait for NTP before trusting it.
- **Paper only.** Nothing here has been validated with real money, and the
  dashboard is protected by a single HTTP Basic credential.
