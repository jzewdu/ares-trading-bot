# Ares Crypto Bot — delta-neutral carry desk

A small Python service for a Raspberry Pi 3 Model A+ that holds a market-neutral
crypto book: long spot, short the matching perpetual, collecting funding. It
runs in paper mode only and shows its book in an authenticated local dashboard.

## The trade

Each pair is two legs of equal notional:

- **long spot** on Alpaca — a live paper order, filled and confirmed,
- **short perpetual** on Hyperliquid (priced at Hyperliquid's mark).

Price moves cancel, so the book has no directional exposure — net delta sits
within a few basis points of zero. What remains is:

1. **funding** paid by the perp every hour to the short when the rate is positive,
2. **basis** — the perp premium over its index, plus any gap between the two venues,
3. **fees** — Alpaca's real crypto fee on the spot leg, 0.045% Hyperliquid taker on the perp.

Realised equity reconciles to those three components exactly; the dashboard
shows the split rather than a single opaque number.

### The long leg is broker truth

Every entry submits a market buy and waits for the fill. The pair is then sized
from **Alpaca's position**, not from the request: Alpaca charges its 25 bps
crypto fee in coin, so the quantity that lands is 0.25% smaller than the order
reports. Sizing the short off the order would leave the hedge silently short.
Each position stores the broker's order id and is reconciled against Alpaca's
positions every cycle; the dashboard shows `alpaca fill: verified` only when the
quantities match within 0.5%. Cash in the ledger *is* the broker's cash, so the
long leg can never drift from the account.

Entries also refuse a thin book: the quoted bid/ask must be inside **0.10%**.
On 2026-10-23 the live quotes were BTC 0.026%, ETH 0.029%, SOL 0.080%, LINK
0.169%, DOGE 0.29%, LTC 0.54% — a market order pays that spread twice, so a
0.54% pair would burn 1% of notional to earn ~9% a year.

## Why this strategy

Measured on **730 days of real hourly funding** (17,520 settlements per coin, 12
coins, Oct 2024 – Oct 2026; `research/fetch_hl.py`). Fees and slippage are
charged on both legs, entries and exits.

| Measure | Result |
|---|---|
| Median funding paid to the short | **10.95% APR** on every coin |
| Holding one hedged pair for two years | **+7.2% to +9.9% annualised** on capital, max drawdown 0.05–0.7% |
| Walk-forward (rank on trailing 180d, hold 90d) | **+5.3% annualised**, 0.74% max DD, Sharpe 4.4 |
| Benchmark, hold BTC + ETH only | **+6.0% annualised**, 0.17% max DD |
| Deployed basket replay (LINK, BTC, ETH) | **+6.8% annualised**, 0.29% max DD |

Two findings shaped the live rules:

- **The edge is structural, not tactical.** Hyperliquid's funding formula carries
  a 0.01%-per-8h interest floor, which is why the median is exactly 10.95% APR.
  Sharpe is high because the return is nearly a coupon, not a trend.
- **Rotating into whatever pays most destroys the edge.** Entering when trailing
  funding exceeded 15% APR and rebalancing daily produced **−8% annualised with
  45% of notional paid in fees**. Any book that rebalances more than about 25
  times a year goes net-negative; the live book therefore holds a fixed basket
  and leaves only when funding turns against it.

## Live configuration

| Setting | Value |
|---|---|
| Basket | LINK, BTC, ETH (`DN_UNIVERSE`) |
| Slots / sizing | 3 slots, 60% of equity notional, 20% per pair (`DN_SLOTS`, `DN_NOTIONAL_PCT`) |
| Short margin | 25% of notional (4×), liquidation near a 25% adverse move |
| Enter | trailing 7d funding above **+2% APR**, perp premium below **2%** and above **−0.25%**, venue gap inside **1%**, Alpaca spot spread under **0.10%** |
| Exit | trailing 72h funding below **−3% APR**, or perp premium beyond **2%** |
| Cadence | re-mark every 5 minutes; funding settles hourly against realised rates |
| Daily stop | entries pause for the UTC day beyond a 3% equity loss; exits keep running |

## Honest limitations

- **Alpaca cannot short crypto.** Its documented crypto API is spot-only and
  marks pairs `shortable: false`; there are no crypto perpetuals or borrow. The
  long leg is a real paper order on the account; the short leg is a **ledger
  position**, marked at Hyperliquid's real mark and settled on Hyperliquid's
  real funding. Nothing is deposited on a derivatives venue, so the short leg's
  P&L and the collected funding are measured, not earned.
- **Funding can turn.** In the sample, 6–33% of hours paid the short nothing or
  charged it, and 30-day funding went as low as −30% APR on the weakest coins.
  The exit rule limits the damage but cannot prevent a settlement gap.
- **The short leg can be liquidated.** At 4× leverage a 25% rally exhausts the
  margin even though the spot leg gains elsewhere; the two legs sit in different
  venues and cannot net margin.
- **Basis is not free.** Cross-venue gaps of 0.1–0.8% appear regularly, usually
  because Alpaca's thinner pairs print rarely. Prices older than five minutes
  are marked on Hyperliquid's index and labelled as such.
- **Paper only.** The build refuses to start unless `ALPACA_PAPER=true`, and
  nothing here has been validated with real money.

## Files

- `engine.py` — carry loop: marks both legs, settles funding, opens/closes pairs.
- `hl_client.py` — read-only Hyperliquid data (funding, premium, marks).
- `alpaca_client.py` — paper Alpaca client plus latest-price freshness checks.
- `strategy.py` — trailing-funding, premium and basis rules.
- `state.py` — SQLite ledger: positions, funding events, equity marks, decisions.
- `app.py` — authenticated dashboard and control endpoints.
- `templates/`, `static/` — dashboard UI.
- `research/` — data fetch, carry study, walk-forward, summary generator.
- `test_engine.py` — accounting test: broker-filled entries, fee-in-coin sizing,
  funding accrual, delta neutrality, exit path, spread gate.
- `migrate_broker_truth.py` — one-off rewrite of open pairs from broker positions.

## Local setup

`cp .env.example .env`, add paper Alpaca keys and dashboard credentials, then
`chmod 600 .env`. Install `requirements.txt` in a virtual environment and run
`gunicorn --workers 1 --threads 4 --bind 127.0.0.1:8080 app:app`. The dashboard
binds to localhost unless TLS certificates are configured; reach it through an
SSH tunnel (`ssh -N -L 8099:127.0.0.1:8080 user@host`).

Run `python test_engine.py` for the accounting check, and
`cd research && python make_summary.py` to regenerate the dashboard's research
panel from the stored funding history (`hl_funding.json.gz`).

## Deployment

Running on the Pi as `ares-bot.service` (gunicorn, state at
`/var/lib/ares/ares_state.sqlite3`). Because the unit is a system service, a code
change needs either `sudo systemctl restart ares-bot` or, without root,
`kill -HUP $(systemctl show ares-bot -p MainPID --value)` — gunicorn re-forks its
worker, which re-imports the app and starts a fresh engine thread against the
same state file. The `HUP` path is what the last deploy used.

Ledger state is not reset by a reload: pairs, funding events and marks survive.
After any change to how positions are sized, run `migrate_broker_truth.py` once
to rewrite open pairs from Alpaca's positions.

Rotate the dashboard password and Alpaca keys that have appeared in chat.
