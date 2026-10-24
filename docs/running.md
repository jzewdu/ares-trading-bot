# Running ARES

> **Updated 2026-10-24.** The service, unit and state path described here are
> unchanged, but the engine now runs the gear-shift desk in the
> [README](../README.md). `.env.example` is the canonical list of settings.
> `systemctl restart ares-bot` still needs root; without it, reload with
> `kill -HUP $(systemctl show ares-bot -p MainPID --value)`.

ARES is one Python process plus gunicorn, serving a localhost dashboard, on a
**Raspberry Pi 3 Model A+**. It is paper-trading only.

## Configuration

Copy the template and fill it in. `chmod 600 .env` — it holds broker keys.

```bash
cp .env.example .env
chmod 600 .env
```

| Variable | Meaning |
| --- | --- |
| `ALPACA_API_KEY` | paper account key id |
| `ALPACA_API_SECRET` | paper account secret |
| `ALPACA_PAPER` | must stay `true`; the client refuses otherwise |
| `ARES_UI_USER` / `ARES_UI_PASSWORD` | dashboard login; unset means the dashboard returns 503 |
| `ARES_BIND` | bind address, default `127.0.0.1` |
| `ARES_TLS_CERT` / `ARES_TLS_KEY` | set both to bind beyond localhost over HTTPS |
| `ARES_STATE_PATH` | SQLite file, default `ares_state.sqlite3` |
| `UNIVERSE` | comma-separated coins, default 12 liquid USD pairs |
| `TREND_ADX`, `TREND_ER`, `RANGE_ADX`, `RANGE_ER`, `RANGE_MIN_WIDTH` | gear thresholds |
| `RISK_PCT`, `MAX_POSITIONS`, `MAX_POSITION_PCT`, `MAX_EXPOSURE_PCT`, `MAX_SPREAD`, `MAX_DAILY_LOSS_PCT` | risk, sizing and cost gates |
| `TREND_STOP_ATR`, `TREND_TARGET_ATR`, `TRAIL_ATR`, `RANGE_STOP_ATR`, `RANGE_TARGET` | exits |
| `POLL_SECONDS` | cycle cadence, default 300 |

`MAX_POSITIONS` and `MAX_POSITION_PCT` are shared names from an earlier build.
If an old `.env` still says `MAX_POSITION_PCT=0.015`, every position is capped
at 1.5% of equity and the desk will look almost idle; the gear-shift values are
`4` and `0.25`.

**The Python code does not read `.env` itself.** `config.py` calls
`os.getenv()` at import, so under systemd the variables arrive via
`EnvironmentFile=`, and for a manual run you must export them first:

```bash
set -a; source .env; set +a
```

Forgetting this is the usual cause of "Setup required" on the dashboard — the
process simply saw no keys.

## Install on the Pi

```bash
sudo apt update && sudo apt install -y git python3-venv
git clone https://github.com/jzewdu/ares-trading-bot.git ~/ares_trading_bot
cd ~/ares_trading_bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env    # then edit .env
```

The unit in this repo expects `/home/jzewdu/ares_trading_bot` and
`User=jzewdu`. Adjust those paths if the Pi's layout changed.

## Run it under systemd

```bash
sudo cp ares-bot.service /etc/systemd/system/ares.service
sudo mkdir -p /var/lib/ares && sudo chown jzewdu:jzewdu /var/lib/ares
sudo systemctl daemon-reload
sudo systemctl enable --now ares
journalctl -u ares -f
```

The unit exists for one reason: `Restart=on-failure` with `RestartSec=10`
brings the service back after a crash. It also sandboxes the process
(`ProtectSystem=full`, `ProtectHome=read-only`, `NoNewPrivileges`), with
`/var/lib/ares` the only writable path — which is why the state database lives
there and not next to the code.

`paused` lives in SQLite. A fresh database starts unpaused; an existing one
keeps whatever the dashboard last set. Pausing blocks entries only — open
positions are still managed and closed.

## Reach the dashboard

It binds `127.0.0.1:8080` and speaks HTTP, so it is only reachable from the Pi.
Use an SSH tunnel from your laptop:

```bash
ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 jzewdu@Pi3-Earth.local
```

On Windows, `scripts/Open Ares Dashboard.cmd` does exactly this — it opens the
tunnel, waits for the port to answer, then launches
`http://127.0.0.1:8080/`. Keep the SSH window open while you use the dashboard.

Do not port-forward 8080 or bind it to the network without TLS. The TLS path
exists (`ARES_TLS_CERT` + `ARES_TLS_KEY`), but the tunnel costs nothing and
exposes nothing.

## The Pi 3 Model A+ as a host

| Spec | Value | What it means here |
| --- | --- | --- |
| CPU | 1.4 GHz quad-core Cortex-A53 | one scan every 300 s is cheap |
| RAM | 512 MB | fine for Flask + gunicorn + requests; the reason there is no pandas/sklearn |
| Network | Wi-Fi only, no Ethernet | the loop depends on Wi-Fi; API errors pause it |
| USB | one port | keyboard or storage, not both |
| Clock | no RTC | time is wrong after a power cut until NTP syncs |
| Storage | microSD | avoid chatty logs; the state database is small |

- **Memory.** `ProtectSystem` and a 1-worker gunicorn keep it modest. If the
  service disappears with no log line, check for the OOM killer:
  `journalctl -k | grep -i oom`. A strategy change that adds heavy analytics
  will not fit.
- **Clock.** The daily-loss baseline resets on the **UTC** date, and bar
  timestamps come from the broker. After a power cut, wait for
  `timedatectl show -p NTPSynchronized --value` to say `yes` before resuming,
  or the 3% daily window starts at the wrong moment.
- **Power.** `vcgencmd get_throttled` returning anything but `0x0` after an
  unexpected reboot means undervoltage — fix the supply, not the code.
- **Journal size.** If the card fills up, cap it:
  `SystemMaxUse=100M` in `/etc/systemd/journald.conf`, then restart
  `systemd-journald`.

## Credentials

The 2026-10-18 rollout plan records that an Alpaca key pair and the Pi password
were pasted into a chat and should be treated as compromised. Before this bot
trades anything:

1. Rotate the Alpaca **paper** keys and put the new ones in `.env`.
2. Change the Pi's password locally, or install an SSH key and disable password
   login.
3. Keep the dashboard credentials long and unique — they are all that stands
   between the network and the pause/resume buttons.

## Updating the code

The repository is the canonical copy of the source. Edit and push from the
machine you develop on, then on the Pi:

```bash
cd ~/ares_trading_bot
git pull
sudo systemctl restart ares
```

If you edited the running copy on the Pi instead, `scripts/publish-from-pi.sh`
copies those edits back into the repo, commits and pushes them, refusing to
stage secrets or a SQLite state file:

```bash
cd ~/ares_trading_bot
scripts/publish-from-pi.sh --dry-run
scripts/publish-from-pi.sh
```
