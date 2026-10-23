# How ARES works

Everything here describes the code in this repository — `engine.py`,
`strategy.py`, `config.py`, `alpaca_client.py`, `state.py`, `app.py`. The
numbers are the defaults in `config.py`.

## What it trades

Crypto on Alpaca, paper account only. Each cycle it asks Alpaca for assets with
`asset_class=crypto`, `status=active`, and keeps symbols ending in `/USD` or
`/USDC`. That list is sorted and truncated to `MAX_PAIRS` (56) — a deliberate
cap so one scan finishes comfortably on a Pi 3 A+. BTC/ETH-quoted crosses are
excluded.

`alpaca_client.py` hardcodes the paper host and raises on construction if
`ALPACA_PAPER` is not `true`. There is no live-trading path in this code.

## The cycle

`Engine._cycle()` runs in a daemon thread, one pass every
`max(60, POLL_SECONDS)` seconds — 300 s by default.

1. **Account check.** Read equity. If it is unavailable or ≤ 0, raise: no
   equity means no sizing, so no orders.
2. **Daily baseline.** On the first cycle of a UTC day, store today's equity as
   the baseline. If equity is now ≤ baseline × (1 − 3%), write a warning event
   and set `paused = 1`.
3. **Universe.** Fetch active crypto assets and filter to the USD/USDC pairs
   described above.
4. **Data.** Fetch 36 hours of 15-minute bars for the whole universe in one call
   (limit 10 000 bars), so the signal has 55+ completed bars per pair.
5. **Exits first.** For each position this bot opened, compare the broker's
   current price to the average entry price. At or below 96% of entry, submit a
   market sell for the full quantity, with the reason `4% protective exit`.
6. **Pause gate.** If `paused` is set, stop here. Exits above have already run —
   pausing means *no new entries*, not "do nothing".
7. **Entries.** For each symbol not already held and not already open: evaluate
   the signal, size the order, submit a market buy.
8. **Sleep** until the next cycle.

## The signal

`strategy.signal_for()` is a long-only breakout, computed on the **closed** bars
only (the newest bar is dropped, since it may be incomplete):

| Component | Rule |
| --- | --- |
| History | needs ≥ 55 bars; every one of the last 55 closes must be > 0 |
| Trend | SMA(20) of closes > SMA(50) of closes |
| Breakout | latest close > the highest high of the previous 20 bars |
| Volume | latest bar's volume ≥ the average volume of those same 20 bars |

All four must hold, otherwise the pair is skipped. It is a filter, not a
prediction: it never sizes up, never shorts, and has no concept of a target
price.

Note what is *not* in this bot despite the earlier v1 description: no RSI, no
MACD, no machine-learning model, no 60-second polling.

## Position sizing

```
per_position = equity × MAX_POSITION_PCT              # 3%
remaining    = equity × MAX_TOTAL_EXPOSURE_PCT − Σ open market value   # 15%
notional     = min(per_position, remaining)
```

Orders under $5 are skipped, and the loop stops opening positions once
`MAX_POSITIONS` (5) is reached. Exposure is measured from the broker's
positions, so on a shared account other positions would count toward the 15% —
this bot expects a dedicated paper account.

## Order identity

Each order gets `client_order_id = ares-<b|s>-<hash(side|symbol|timestamp)>`.
Buy timestamps are the signal bar's time, so a retry of the same bar's signal
cannot place a second buy; Alpaca deduplicates on the client order id, and the
`trades` table's primary key enforces it locally too.

## Pause semantics

`paused` is a value in SQLite, initialised to `'1'` — **the bot boots paused**
and stays that way until someone presses *Resume paper trading* in the
dashboard. Pausing blocks entries and nothing else: protective exits keep
running.

Any exception during a cycle logs an `error` event, sets `paused = 1`, and
changes the status to "Paused after API or data error; inspect logs before
resuming". That is deliberate (fail closed) but blunt: a single transient
network failure needs a human to resume. Check the Activity panel before
blaming the strategy.

## State

`state.py` is plain SQLite (`ares_state.sqlite3`, or `ARES_STATE_PATH`):

- `events` — rolling window of the last 100 log lines shown in the dashboard.
- `trades` — one row per submitted order; primary key is the client order id.
- `kv` — `paused`, `baseline_date`, `baseline_equity`.

"Which positions are mine?" is answered from `trades`: the latest row per
symbol, if its side is `buy`. Exits only apply to those symbols, so a position
you open by hand is not auto-sold.

## Dashboard

`app.py` starts the engine on import and serves:

| Route | What it does |
| --- | --- |
| `GET /` | status, scanned pair count, recent orders, activity |
| `GET /api/state` | the same as JSON |
| `POST /control/pause` | pause new entries |
| `POST /control/resume` | resume entries (refuses without paper + dashboard creds) |

HTTP Basic auth on every route, compared with `hmac.compare_digest`; if the
dashboard credentials are unset the app answers 503 instead of running open.
Control posts must also carry `X-Ares-Action: dashboard`. The page refreshes
every 30 seconds.

Binding is `127.0.0.1` unless both `ARES_TLS_CERT` and `ARES_TLS_KEY` are set —
see [running.md](running.md) for how access works from another machine.

## Known gaps

- **No backtest or walk-forward harness.** The rollout plan calls for one; the
  code does not contain it. Paper results are the only evidence so far.
- **Exits are polled.** The 4% stop is checked once per cycle and fills at
  market, so a fast move can fill far below 96% of entry.
- **The plan promised checks the code does not make.** `docs/rollout-plan.md`
  lists stale-data and unhealthy-clock guards; `engine.py` has neither. Bar
  freshness is not compared to the wall clock, so a stalled data feed is not
  currently detected as such.
- **Fail-closed is manual.** Any cycle error pauses the bot until resumed by
  hand — safe, but it means uptime depends on someone noticing.
- **Ownership is inferred** from the local `trades` table rather than broker
  metadata, so a wiped state file makes the bot treat its own positions as
  foreign (it will not exit them).
