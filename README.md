# ARES — ares-trading-bot

ARES is a **paper-only** crypto trading bot for a Raspberry Pi 3 Model A+. Every
five minutes it scans Alpaca's active USD/USDC pairs for a 15-minute momentum
breakout, opens at most five small positions, and exits any position that falls
4% below its entry. It starts paused, pauses itself at a 3% daily loss, and
reports through a password-protected local dashboard.

It cannot trade live: the Alpaca client refuses to start unless
`ALPACA_PAPER=true`, and the paper host is hardcoded.

> Educational project, paper money only. Not financial advice.

## The loop

```
every 300s (POLL_SECONDS)
   │
   ├─ 1. account equity → pass on it, or pause if today's baseline -3%
   ├─ 2. list active Alpaca crypto assets, keep /USD and /USDC pairs
   ├─ 3. fetch 36h of 15-minute bars for those pairs (capped by MAX_PAIRS)
   ├─ 4. defensive exits first: any owned position ≤ entry × 0.96 → market sell
   ├─ 5. if paused → stop here (exits above still ran)
   ├─ 6. entries: 20-bar breakout + 20/50 SMA trend + volume confirmation,
   │      sized to 3% of equity, capped by 15% total exposure
   └─ 7. sleep 300s
```

Full detail, including the signal maths: [docs/how-it-works.md](docs/how-it-works.md).

## Risk limits

These are the defaults in `config.py`, and every one is enforced in code:

| Limit | Value | Behaviour |
| --- | --- | --- |
| Per position | 3% of equity | order notional is clamped to this |
| Total exposure | 15% of equity | new entries stop at the cap |
| Daily loss | 3% of equity | pauses new entries until you resume |
| Open positions | 5 | new entries stop at the cap |
| Pairs scanned | 56 | keeps the Pi's five-minute cycle affordable |
| Protective exit | −4% from entry | market sell, checked every cycle |
| Poll interval | 300 s (floor 60 s) | one full scan per cycle |

## Repository map

```
.
├── app.py                  Flask dashboard, Basic auth, pause/resume endpoints
├── engine.py               the loop: scan → exits → entries, risk checks
├── strategy.py             the 15-minute breakout signal
├── alpaca_client.py        paper-only REST calls (market data + orders)
├── config.py               env-driven settings
├── state.py                SQLite: events, trades, key/value state
├── templates/, static/     dashboard UI
├── ares-bot.service        systemd unit for the Pi
├── requirements.txt        Flask, requests, gunicorn
├── docs/
│   ├── how-it-works.md     signal, sizing, exits, pause semantics
│   ├── running.md          Pi 3 A+ setup, systemd, dashboard access
│   ├── build-loop.md       how this was built, in order
│   └── rollout-plan.md     the original 2026-10-18 build/rollout plan
└── scripts/
    ├── publish-from-pi.sh        push edits made on the Pi back into git
    └── Open Ares Dashboard.*     Windows SSH-tunnel launcher for the dashboard
```

## Running it

Setup, environment variables, the systemd unit and dashboard access:
[docs/running.md](docs/running.md).

Short version on the Pi:

```bash
git clone https://github.com/jzewdu/ares-trading-bot.git ~/ares_trading_bot
cd ~/ares_trading_bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env     # then fill in fresh paper keys
sudo systemctl enable --now ares
```

Then open the dashboard through an SSH tunnel (`scripts/Open Ares Dashboard.cmd`
on Windows does this for you).

## History

| When | What happened |
| --- | --- |
| 2026-10-09 | **v1** — stock bot, RSI/MACD/SMA, 60s poll, 2% risk / 5% daily stop; Claude Code via Ollama, hosted on PythonAnywhere |
| After v1 | **Codex** reviewed and refactored the v1 loop |
| 2026-10-18 | **v2** — this code: paper-only Alpaca crypto breakout bot, dashboard, SQLite state, systemd unit |
| Then | Deployed to the **Raspberry Pi 3 Model A+** at home |

Details, and what is honestly unknown: [docs/build-loop.md](docs/build-loop.md).

## Disclaimer

Built with AI coding agents. Paper money only, no warranty, not financial
advice. The strategy is a research candidate: it has not been validated
out-of-sample and it can lose money in a live market.
