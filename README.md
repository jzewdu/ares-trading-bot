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

`research/btc_bt.py` runs the exact live rule over the last **240 days of real
Alpaca 4h BTC bars**, charging both fees and slippage:

| Measure | Result |
| --- | --- |
| Trades | **19** over 240 days (≈ 0.6/week) |
| Win rate | **11%** — trend following wins rarely and pays big when it does |
| Average net R | **+0.22R** |
| Return on equity | **+8.18%** |
| Max drawdown | **6.35%** |
| Time in the market | **66%** |
| Fees paid | ~0.5% per round trip, 19 round trips |

Three caveats, stated plainly:

1. **A single sample.** 19 trades and one 240-day window is not statistical
   proof. The window is a bull-ish market; the brake's value shows up in the
   bear case this sample does not contain.
2. **It underperforms simply holding.** +8.18% versus +22.4% for buy-and-hold.
   The brake bought a 6.35% max drawdown instead of a much larger one. If you
   want maximum upside, holding wins; the desk is insurance, not alpha.
3. **A green night is roughly a coin flip.** In the replay, 33% of 12-hour
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
  decay, broker-backed entry, stop exit, fee accounting and spread gate.
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
