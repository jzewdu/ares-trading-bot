<!-- Preserved as written on 2026-10-18, as the plan for v2. Its "Deployment
status" section reflects that day, not the current state; the code it describes
now lives in this repository, and docs/running.md covers operations. -->

# Ares Crypto Bot — build and rollout plan

## Goal

A small Python service for a Raspberry Pi 3 Model A+ that continuously watches Alpaca's active USD and USDC crypto pairs, shows its status in a browser dashboard, and begins in paper-trading mode. Alpaca's supported pairs are a limited universe; it cannot trade every cryptocurrency in existence. Cross-crypto pairs are excluded from this first build.

## Strategy candidate

Start with a long-only, spot-market momentum breakout: rank eligible USD-quoted pairs by recent momentum, require a breakout above a rolling high and a volume confirmation, then enter only when the broad market trend is positive. Exit on a trailing stop or loss of trend. This is a testable hypothesis, not a proven profit engine: momentum can fail abruptly, crypto trades continuously, and fees, spread, slippage, outages, and gaps can erase gains. The strategy should be evaluated with out-of-sample walk-forward tests and paper trading before any live use.

## Safety defaults

- Paper trading only. Never silently switch to the live Alpaca host.
- No leverage, shorting, withdrawals, or transfers.
- Explicit allowlist of active Alpaca USD pairs; refresh it from Alpaca and cap the number scanned to fit the Pi.
- Risk limits are enforced in code: per-position size, total exposure, daily loss stop, maximum open positions, and a global stop switch for new entries. Protective exits continue while entries are paused.
- Stop creating orders when data is stale, the API is unavailable, account state cannot be reconciled, or the clock is unhealthy.
- Secrets come from a local environment file with restrictive permissions and are never placed in source control or the dashboard.
- Dashboard binds to localhost unless HTTPS certificate and key paths are configured. Do not port-forward it or expose it to the public internet.
- Live mode requires a separate deliberate configuration change and paper-mode history review. It is not part of the initial rollout.

## Build stages

1. **Prepare** — install a current 64-bit Raspberry Pi OS Lite image, update it, set a unique password, use Ethernet if available (the Model A+ has Wi-Fi but no Ethernet), and reserve a DHCP address. Keep the Pi and microSD storage healthy; power/network interruptions are expected failure cases.
2. **Paper MVP** — install a lightweight Python web service, strategy loop, persistent state/logs, and dashboard. The dashboard requires a username/password and binds to localhost. Access it from another device through an SSH tunnel.
3. **Validate** — check supported assets and minimum order sizes; run historical simulations with fees/slippage assumptions, walk-forward checks, then paper trading for at least several weeks. Compare every simulated fill and position against the broker account.
4. **Operate paper mode** — monitor uptime, stale-data and API errors, daily P&L, exposure, and stop-switch behavior. Restart recovery must reconcile open orders and positions before the strategy resumes.
5. **Optional live review** — only after separate user approval, newly rotated credentials, and successful paper results. Begin with very small caps and keep a manual stop switch. No return is guaranteed.

## Initial architecture

- `app.py`: dashboard and local control endpoints.
- `engine.py`: market scan, strategy decisions, risk checks, and order lifecycle.
- `alpaca_client.py`: isolated Alpaca paper API access.
- `state.py`: durable local state and restart reconciliation.
- `templates/` and `static/`: compact responsive UI.
- `.env.example`: safe paper defaults; actual keys and dashboard secrets are supplied separately on the Pi.
- `ares-bot.service`: restart-on-failure systemd unit template. It expects the Pi project at `/home/jzewdu/ares_trading_bot` and TLS files at `/etc/ares/`.

The Pi 3 Model A+ has a 1.4 GHz quad-core CPU and 512 MB RAM. Keep the service single-process and avoid heavyweight analytics databases or machine-learning models. A browser dashboard can be opened from another device on the same trusted network.

## Current starter limitations

- The first strategy is a research candidate, not independently validated. It buys only on a simple breakout signal, checks a 4% exit every polling cycle, and pauses when the account's daily equity loss reaches 3%. A polling exit can be delayed by outages or price gaps.
- Orders use Alpaca paper trading only. The code refuses to start if `ALPACA_PAPER` is not `true`.
- It scans active Alpaca USD and USDC pairs, capped at `MAX_PAIRS`; it does not scan BTC/ETH-quoted crosses or unsupported coins.
- Use a dedicated paper account and do not reuse the bot's API key for other software. Rotate the key pair and Pi password that were pasted into chat before deployment.

## Local setup

Copy `.env.example` to `.env`, enter newly created **paper** keys and unique dashboard credentials, then restrict the file to its owner (`chmod 600 .env`). Install Python dependencies in a virtual environment and run `gunicorn --workers 1 --threads 4 --bind 127.0.0.1:8080 app:app`. The dashboard is available only on the Pi by default. Before allowing home-network access, install trusted HTTPS certificate/key files and configure a secure bind; never use plain HTTP or public port forwarding.

The systemd unit is a deployment template and expects its project and service account paths to be checked on the Pi before installation. The dashboard binds to localhost; use an SSH tunnel for access from another device.

The previous folder contents were cleared at the user's direction. This plan and paper-only starter are ready for review. Deployment is not yet configured: the Alpaca key pair and Pi password previously pasted into chat must be rotated before they are used. Do not reuse them.

## Deployment status

The bot files are prepared in the project folder, but they have not been installed or started on the Pi. Deployment must wait for fresh paper API credentials and a new Pi authentication method; the credentials shared in chat are considered compromised. Use SSH key authentication or change the Pi password locally, then provide a safe way to reach the Pi. The initial service must remain paused until you review the account and explicitly press **Resume paper trading**.
