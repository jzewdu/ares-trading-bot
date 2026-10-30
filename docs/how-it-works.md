# How ARES works

The live rules, defaults and replay results are in the [README](../README.md).
This page is the implementation detail.

## The cycle

`engine.py` runs `Engine._cycle()` in a daemon thread every
`max(60, POLL_SECONDS)` seconds (300 s by default).

1. **Quotes.** `latest_market()` returns a trade or quote per pair with its age
   and quoted spread. A stale print still marks the book but cannot open a
   trade.
2. **Bars.** `bars(timeframe=BAR_TIMEFRAME)` pages the requested candles back
   `BARS_DAYS` and, for intraday feeds, folds them into 4-hour buckets. For the
   shipping daily feed nothing is aggregated: a daily candle is already the
   resolution the 200-bar line is meant to read.
3. **Rule.** `btc_trend.analyse()` walks the candles once, computing a 200-bar
   EMA, ATR(14), ADX, RSI, Bollinger bands and Donchian levels, and marks each
   candle `trend` (close above the brake), `trend/flat` (below the line but
   inside the band) or `stand_aside/down` (past the brake). Warm-up is
   `EMA + 10` candles and is skipped, never back-filled.
4. **Mark.** Equity and cash come from Alpaca. Exposure and open P&L are marked
   from the latest quotes. The daily-loss baseline is stored per UTC date.
5. **Exits.** The catastrophe stop (8 ATR below the fill) and the brake are
   checked every cycle; the brake is a *close* condition, so it is evaluated from
   the rule's own state rather than from the live tick.
6. **Entries.** Candidates are ranked by signal score and capped at
   `MAX_POSITIONS`. Each must clear the spread gate, the freshness check, the
   cooldown, the re-arm gate, the learner's avoid gate, the foreign-position
   guard and the portfolio caps.
7. **Reconcile and record.** The ledger is compared with `GET /v2/positions`,
   equity is marked and one row is written to `gear_marks`.

## The rule

One instrument-level state machine, no regimes and no gears:

| Condition | Action |
| --- | --- |
| close > EMA(200) − 3 ATR | hold or enter the core long |
| close < EMA(200) − 3 ATR | sell, write a re-arm anchor, wait |
| any exit, price < brake + 1.5 ATR | refuse a new entry |
| price ≤ entry − 8 ATR | catastrophe stop, intracycle |

A position is entered with a limit-free market order sized so that the distance
from fill to catastrophe stop is exactly 2% of equity:

```
qty = equity * RISK_PCT / (TREND_STOP_ATR * ATR)
```

Because an 8-ATR stop on a daily candle is wide (roughly 15-30%), that formula
deploys 8-13% of equity per name. Four names plus the 50% exposure cap bound the
book at about half of equity.

## Order lifecycle

`market_buy(notional)` is submitted with a unique `client_order_id`, then
`wait_fill()` polls until the order is `filled`, `canceled`, `rejected`,
`expired` or `replaced`. Nothing enters the ledger unless the fill is real and
a broker position exists.

The trade is sized from the **broker's quantity**, not the requested notional:
Alpaca takes its crypto fee in coin, so the position is 0.25% smaller than the
order. The stop and target are anchored to the actual `avg_entry_price`, which
preserves the risk geometry through slippage.

Exits sell `min(ledger quantity, broker quantity)`, formatted by
`qty_string()` which truncates to 8 decimals. Rounding up past the balance is
what once made Alpaca answer 403 `insufficient balance` and stranded a LINK leg.
Selling the ledger's own quantity rather than the broker's whole position means
a second process on the same account cannot have its trades liquidated by this
one.

## Re-arm anchors

A brake exit is not a dip to buy back immediately. On every exit the engine
writes `rearm_<COIN> = "<iso timestamp>|<reason>"` to `kv`, and clears it on the
next successful entry. `_enter()` reads the anchor when `recent_close()` has no
row, so a restart on a fresh database still knows the account owes the market a
re-arm.

This exists because it failed in production. The gate tested
`exit_reason.startswith("trend brake")`, so an exit recorded for any other reason
— a handover, a manual flatten — carried no anchor and the book bought straight
back. The account sold at 81,124 and re-bought at 81,290 ten minutes later, then
again at 80,150 after a 79,840 brake exit: about $1.1k in two round trips.

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
| `decisions` | why a candidate was bought, skipped or refused |
| `events` | rolling 120-line system log |
| `kv` | `paused`, launch baseline, daily-loss baseline, **re-arm anchors** |

The retired carry tables (`dn_*`) may still exist in the database; nothing reads
them.

## Configuration precedence

`.env` is loaded at import with `os.environ.setdefault`, so a value already in
the real environment wins. That ordering is deliberate: the test suite sets
`ARES_STATE_PATH` to a temp file, and with the file overriding it the suite
wrote synthetic trades into the live book.

## Failure behaviour

A failed cycle logs an error and keeps the book. After six consecutive failed
cycles, entries pause; exits resume as soon as data returns. A daily loss beyond
3% pauses entries for the UTC day. `scripts/run_ares.cmd` restarts a dead process
after 15 seconds and refuses to start a second copy while the dashboard port is
listening. Nothing in this path places a second order for a signal that already
filled: one position per coin, plus a cooldown after close.
