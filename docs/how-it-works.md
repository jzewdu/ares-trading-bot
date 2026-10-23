# How ARES works

## The loop

ARES is a single always-running loop. One pass every 60 seconds:

1. **Poll.** Read the newest data it needs from Alpaca for each symbol on its
   watchlist.
2. **Compute.** Build the indicator values the strategy uses — RSI, MACD and
   SMA (simple moving average).
3. **Decide.** Feed those into the model/rule set and get a signal: take a
   position, hold, or go flat.
4. **Risk gate.** Reject or shrink the order if it breaks a risk limit
   (below). This runs *after* the signal, so no signal can bypass it.
5. **Execute.** Send the order to Alpaca and record what was decided and why.
6. **Sleep** 60 seconds and start again.

Because the loop is stateless between passes, a crash costs one cycle, not the
whole day — it restarts by re-reading market data and current positions.

## Indicators

| Indicator | What it contributes |
| --- | --- |
| RSI | Momentum / overbought-oversold reading |
| MACD | Trend direction and momentum shifts |
| SMA | Baseline trend and price-vs-average context |

The v1 design feeds these values into a model rather than trading a single
hand-written rule, so the indicator set is the feature set. The exact
estimator, window lengths and thresholds live in the bot source on the Pi
and should be documented here once the source is published.

## Risk controls

Two hard limits, both cheaper than the alternative:

- **2% risk per trade.** Position sizing is derived from the distance to the
  stop, so the worst case on any one trade is ~2% of account equity. A wider
  stop therefore buys a *smaller* position, not a bigger loss.
- **5% daily stop loss.** Once the day's drawdown reaches 5%, ARES stops
  opening new trades and waits for the next session. This caps a bad day
  instead of letting one bad loop compound.

These are the v1 numbers and the values to keep in sync with the code:

| Limit | Value |
| --- | --- |
| Risk per trade | 2% of equity |
| Daily stop loss | 5% of equity |

The constant names in the source may differ; the numbers must not.

## What it deliberately does not do

- No leverage, no shorting complexity beyond what the signal supports.
- No live-money endpoint in v1 — paper trading only.
- No mid-loop strategy changes: behaviour is fixed at start-up, so a run is
  reproducible from its config and logs.

## Known gaps

- The Python source, logs and any trained model artefacts are not in the repo
  yet (they are on the Pi).
- No backtest harness in v1; correctness is judged from paper-trading results
  and logs.
