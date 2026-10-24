# Ares — gear-shift crypto desk

A small Python service for a Raspberry Pi 3 Model A+ that reads the market on
closed 4-hour bars, selects a **gear** — trend, range, or stand aside — trades
the setup that fits it, and then scores every closed trade so the next one of
its kind is sized up, sized down, or refused.

Paper only. Every entry and exit is a real order on the Alpaca paper account.

## The three gears

| Gear | Test on the last closed 4h bar | What it trades |
| --- | --- | --- |
| **Trend** | ADX ≥ 25, Kaufman efficiency ≥ 0.30, EMA20 at least 0.35% above EMA60 and price above EMA60 | Buy a fresh break of the prior 20-bar high, trail 4.5 ATR |
| **Range** | ADX ≤ 22, efficiency ≤ 0.22, Bollinger width ≥ 1.5% | Buy the lower band with RSI ≤ 35, sell the middle band |
| **Stand aside** | Anything else, **and every downtrend** | Nothing — the spot account cannot short |

The signal uses only completed bars. A breakout that has already run more than
0.75 ATR past the old high is a chase and is skipped, which is what keeps the
stop close enough for the reward that is left.

## The learner

Each close files the trade under four nested buckets, most specific first:

```
trend|BTC|adx=24-32|vol=0.8-1.5
trend|BTC
trend|adx=24-32|vol=0.8-1.5
trend
```

Each bucket stores a decayed count, win count and sum of **net R** (profit
divided by what the stop risked, after both fees). A candidate is scored against
the most specific bucket with at least 5 trades, falling back to its parents
when there is not enough evidence.

| Evidence | Behaviour |
| --- | --- |
| < 5 trades | neutral, **1.0×** size — the desk keeps exploring |
| 5+ trades, positive expectancy | up to **1.4×** size |
| 5+ trades, expectancy below +0.05R | down to **0.35×** size |
| 8+ trades, expectancy ≤ 0 | **avoid** — the bucket is refused |

Evidence decays with a 30-day half-life, on read as well as on write, so a
bucket that stops trading slowly loses its authority instead of freezing. The
prior is +0.10R with a weight of 6 pseudo-trades: one bad trade cannot block a
setup, and one lucky trade cannot promote it.

## Risk

- **0.75% of equity at the stop** for every trade, before the learner multiplier.
- Stop distance: 3 ATR in trend, 2 ATR in range. Position size is risk ÷ stop
  distance, capped at **25% of equity** per position and **75%** in total.
- At most **4** open positions; one per coin; a 60-minute cooldown after a close.
- Trend exits: 9 ATR target, 4.5 ATR trailing stop, 20-day time stop, or an
  established downtrend. Range exits: middle band, 2 ATR stop, 6-day time stop,
  or the range breaking into a trend.
- Entries pause for the rest of the UTC day after a 3% equity loss. Exits never
  pause.
- A spot spread above **0.15%** blocks an entry, and a quote older than five
  minutes cannot be traded against.

## What the replay says

The same regime, signals, stops, exits and fees, replayed over the last **120
days of real Alpaca 4h bars** across the 12-coin universe:

| Measure | Result |
| --- | --- |
| Trades | **28** (≈ 1.6/week) |
| Win rate | **46%** |
| Average net R | **+0.53R** |
| Return on equity | **+11.48%** over 120 days |
| Max drawdown | **4.53%** |
| Fees modelled | 0.25% per side + 0.05% slippage per side |
| Trend gear | 26 trades, 46% win, +0.53R |
| Range gear | 2 trades, 50% win, +0.47R |

Two honest caveats. The window is a single 120-day sample, and the thresholds
above were chosen on it, so **this is an in-sample calibration, not a
walk-forward result** — treat +11% as an upper bound, not a forecast. And the
range gear barely traded in this sample; its numbers are two trades, not
evidence of an edge.

The learner is not in the replay: the live desk starts neutral and only earns
its multipliers from its own closed trades.

## What is real

Every buy and sell is a market order on Alpaca's paper account, submitted and
polled to a terminal fill. Positions are sized from the broker's own quantity
(a buy of notional `N` receives `N − 0.25%` of coin), and every cycle compares
the ledger against `GET /v2/positions`. Equity is Alpaca's equity; cash is
Alpaca's cash; P&L is net of the fees actually charged. Positions worth less
than a dollar are treated as dust, not as positions.

Alpaca cannot short crypto or trade perpetuals, so the desk is **long-only** and
stands aside in downtrends. There is no leverage, no live-money path, and the
client refuses to start unless `ALPACA_PAPER=true`.

## Files

- `regime.py` — 4h aggregation, ATR/ADX/RSI/Bollinger/Donchian/efficiency, gear
  classifier.
- `gears.py` — the breakout and band-fade signals with their stops and targets.
- `learning.py` — decayed bucket statistics, the sizing multiplier and the
  avoid gate.
- `engine.py` — the loop: analyse, manage exits, size and submit entries,
  reconcile against the broker, record marks.
- `state.py` — SQLite: trades, equity marks, decisions, events, learned buckets.
- `alpaca_client.py` — paper Alpaca REST client, paged bars, fill polling,
  truncating quantity formatting.
- `app.py`, `templates/`, `static/` — authenticated dashboard.
- `test_engine.py` — regime classification, signal geometry, learner avoidance
  and decay, broker-backed entry, stop exit, fee accounting and spread gate.
- `research/gears_bt.py` — the replay and threshold grid that produced the table
  above (`gear_summary.json` feeds the dashboard panel).
- `scripts/retire_carry.py` — the one-off used to liquidate the retired
  delta-neutral book and set the new baseline.

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
.venv/bin/python research/gears_bt.py --days 120
.venv/bin/python research/gears_bt.py --days 120 --grid   # sensitivity
```

## Known limits

- **In-sample replay.** 120 days, one parameter set chosen on that window. No
  walk-forward yet; the live learner is the out-of-sample test.
- **Fees are the main enemy.** 0.50% round trip means a 3 ATR stop is roughly
  0.17R of cost. Tighter stops or more frequent trading turn the edge negative.
- **The learner needs samples.** Until a bucket has 5 closed trades it trades at
  1.0×; until 8 it cannot refuse anything. Early behaviour is the raw rules.
- **Spot-only.** Downtrends are stood aside, not shorted. A market that only
  falls produces no trades.
- **Gap risk.** Stops are checked every five minutes and sell at market, so a
  violent move can fill well through the stop.
- **Paper only.** Nothing here has been validated with real money, and the
  dashboard is protected by a single HTTP Basic credential.
