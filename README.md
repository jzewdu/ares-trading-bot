# ARES — ares-trading-bot

ARES is an automated trading bot that polls market data every 60 seconds and
executes orders through the Alpaca API. It reads RSI, MACD and SMA, turns them
into a signal, sizes each trade against a fixed risk budget, and stops for the
day if it loses too much.

**Status:** v1 built 2026-10-09. Paper trading only. Runs on a Raspberry Pi 3
Model A+ at home; PythonAnywhere before that.

> Educational project, not financial advice.

## How it works

```
every 60s
   │
   ├─ 1. pull latest bars/quote from Alpaca
   ├─ 2. compute indicators (RSI, MACD, SMA)
   ├─ 3. model/rule evaluates them → signal (long / flat)
   ├─ 4. risk check: 2% per trade, 5% daily loss limit
   ├─ 5. submit order via Alpaca, log the decision
   └─ 6. sleep 60s, repeat
```

Full detail, including the risk math: [docs/how-it-works.md](docs/how-it-works.md).

## How it runs

| Phase | Host | Notes |
| --- | --- | --- |
| Now | Raspberry Pi 3 Model A+ | always-on systemd service, 512 MB RAM, Wi-Fi, `.env` for keys |
| Before | PythonAnywhere | always-on task against the Alpaca **paper** endpoint; now the fallback |

Setup for both hosts: [docs/running.md](docs/running.md).

## Repo map

```
.
├── README.md              ← you are here
├── docs/
│   ├── how-it-works.md    ← strategy loop, signals, risk controls
│   ├── running.md         ← PythonAnywhere + Raspberry Pi deployment
│   └── build-loop.md      ← how v1 was built with Claude Code / Codex
├── scripts/
│   └── publish-from-pi.sh ← publishes the Pi's bot files into this repo
├── .env.example           ← required environment variables
└── .gitignore
```

The bot's Python source is not in this repository yet — it lives on the Pi.
`scripts/publish-from-pi.sh` is how it gets here; see
[Getting the code into this repo](#getting-the-code-into-this-repo).

## Getting the code into this repo

The source files are on the Raspberry Pi. From a shell **on the Pi**:

```bash
curl -fsSL https://raw.githubusercontent.com/jzewdu/ares-trading-bot/main/scripts/publish-from-pi.sh \
  -o publish-from-pi.sh
chmod +x publish-from-pi.sh
cd ~/ares          # wherever the bot's .py files live
~/publish-from-pi.sh
```

The script clones (or updates) the repo into `~/ares-trading-bot`, copies your
bot files in, refuses to stage secrets, commits and pushes. It never deletes
anything on either side. Auth setup and the manual fallback are in
[publish-from-pi.sh](scripts/publish-from-pi.sh) and
[docs/running.md](docs/running.md#publishing-code-from-the-pi).

## Build history

- **2026-10-09 — v1:** built with Claude Code running through Ollama.
- **Later:** Codex reviewed and refactored the loop.
- **2026-10-23 — repo:** organized by Codebuff into this docs structure, with
  the Pi publish script. Documentation and tooling only; no trading logic
touched.

Narrative and the actual prompt/test loop: [docs/build-loop.md](docs/build-loop.md).

## Disclaimer

Written with AI coding agents. Educational project, paper money, no warranty.
Nothing here is financial advice.
