# How ARES works

The live rules, defaults and replay results are in the [README](../README.md).
This page is the implementation detail.

## The cycle

`engine.py` runs `Engine._cycle()` in a daemon thread every
`max(60, POLL_SECONDS)` seconds (300 s by default).

1. **Quotes.** `latest_market()` returns a trade or quote per pair with its age
   and quoted spread. A quote older than five minutes is still used to mark the
   book, but it cannot open a trade.
2. **Bars.** `bars()` pages hourly Alpaca bars back `BARS_DAYS` (90) and the
   regime module folds them into 4-hour buckets. The final, partial bucket is
   dropped, so every signal uses a completed bar.
3. **Regime.** For each coin, `classify()` computes EMA20/EMA60, ATR, ADX, RSI,
   Bollinger bands, the prior 20-bar high/low and Kaufman efficiency, then
   assigns a gear. `stable` counts consecutive bars in the same gear and
   direction; signals require at least two.
4. **Mark.** Equity and cash come from Alpaca. Exposure and open P&L are marked
   from the latest quotes. The daily-loss baseline is stored per UTC date.
5. **Exits.** Stops and targets are checked against the live price; a trend stop
   trails 4.5 ATR under the high since entry. A regime flip, a time stop or a
   hit level sells the broker's exact quantity.
6. **Entries.** Candidates are ranked by signal score. Each must clear the
   spread gate, the freshness check (price within 0.75 ATR of the signal close),
   the cooldown, the learner's avoid gate and the portfolio caps.
7. **Reconcile and record.** The ledger is compared with `GET /v2/positions`,
   equity is marked and one row is written to `gear_marks`.

## The classifier

All indicators are Wilder/standard implementations in `regime.py`, computed from
completed bars only.

| Input | Meaning here |
| --- | --- |
| ADX(14) | strength of directional movement, regardless of sign |
| Efficiency(20) | net move ÷ path walked; 1.0 is a straight line, 0 is chop |
| EMA20 − EMA60 | trend separation as a percent of price |
| Bollinger(20, 2) | band width for range quality, lower band for entries |
| Donchian(20) | prior 20-bar high, excluding the current bar |
| ATR(14) | stop distance and position sizing |
| RSI(14) | oversold confirmation in the range gear |

A downtrend is a valid trend, but the account is spot-only, so `direction=down`
maps to **stand aside** rather than a short.

## Signals

**Trend/breakout.** Requires gear `trend`, direction up, the close above the
prior 20-bar high, and the breakout no more than 0.75 ATR beyond it. Stop 3 ATR,
target 9 ATR, trailing stop 4.5 ATR from the high since entry, 20-day time stop.
Score weighs efficiency, ADX and how fresh the break is.

**Range/band fade.** Requires gear `range`, RSI ≤ 35, the close within 0.25 ATR
of (or below) the lower band, and a target at least 1.2 ATR away. Stop 2 ATR,
target the middle band (or the upper band with `RANGE_TARGET=upper`), 6-day time
stop.

Both signals are rejected outright if the stop distance is non-positive or the
reward/risk is below the gear's floor.

## Order lifecycle

`market_buy(notional)` is submitted with a unique `client_order_id`, then
`wait_fill()` polls until the order is `filled`, `canceled`, `rejected`,
`expired` or `replaced`. Nothing enters the ledger unless the fill is real and
a broker position exists.

The trade is sized from the **broker's quantity**, not the requested notional:
Alpaca takes its crypto fee in coin, so the position is 0.25% smaller than the
order. The stop and target are anchored to the actual `avg_entry_price`, which
preserves the risk geometry through slippage.

Exits sell the broker's own quantity **string** (`"1633.614775495"`). Formatting
that to 8 decimals rounds up past the balance — Alpaca answers with a 403
`insufficient balance`, which is exactly how the retired carry book's LINK leg
got stranded once.

## The learner

`learning.py` writes four buckets per closed trade and reads the most specific
one with enough evidence. The score is the posterior mean R with a prior of
+0.10R at weight 6. Decay is `0.5 ** (days / 30)`, applied both when a trade is
added and when a bucket is read.

Sizing: `0.75 + expectancy × 2.0`, clamped to 0.35×–1.4×, and **zero** once a
bucket has 8 trades and non-positive expectancy. The multiplier scales the risk
of the next trade, not the stop distance.

Every close writes a human-readable lesson onto the trade row (`... | 8 trades,
10% win, -0.53R learned expectancy`) and the dashboard's journal shows it.

## State

`state.py` is plain SQLite:

| Table | Contents |
| --- | --- |
| `gear_trades` | one row per trade, entry/exit cash, fees, R, lesson, broker ids |
| `gear_marks` | equity, exposure, open count and active gear each cycle |
| `learn` | decayed `n`, `wins`, `sum_r` per bucket |
| `decisions` | why a candidate was bought, skipped or trailed |
| `events` | rolling 120-line system log |
| `kv` | `paused`, launch baseline, daily-loss baseline |

The retired carry tables (`dn_*`) may still exist in the database; nothing reads
them.

## Failure behaviour

A failed cycle logs an error and keeps the book. After six consecutive failed
cycles, entries pause; exits resume as soon as data returns. A daily loss beyond
3% pauses entries for the UTC day. Nothing in this path places a second order for
a signal that already filled: one position per coin, plus a cooldown after close.
