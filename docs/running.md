# Running ARES

ARES is a long-lived Python process with one job: stay up and run the 60-second
loop. Everything below keeps the process alive, keeps the keys out of git, and
keeps it pointed at the paper endpoint.

## Configuration

Copy `.env.example` to `.env` and fill it in. Never commit `.env`.

| Variable | Purpose |
| --- | --- |
| `ALPACA_API_KEY` | Alpaca key id |
| `ALPACA_SECRET_KEY` | Alpaca secret |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` for paper trading |

Variable names in `.env.example` are the target convention — if the bot source
on the Pi uses different names, rename them there rather than in two places.

## Phase 1 — PythonAnywhere (current)

1. Upload the bot files (or `git clone` the repo once the source is pushed).
2. Create the virtualenv and install the bot's requirements.
3. Set the three variables above in the **Web** or **Task** environment, or in
   a `.env` the process reads — not in the source.
4. Run it as an **Always-on task** so it restarts if it exits:
   `python3 bot.py >> /tmp/ares.log 2>&1`
5. Confirm paper mode by checking that orders appear on the Alpaca paper
   dashboard, never the live one.

Caveat: PythonAnywhere always-on tasks get recycled. If the loop dies silently,
check `/tmp/ares.log` before assuming a strategy bug.

## Phase 2 — Raspberry Pi (planned)

The Pi is the better home: it stays up, it is yours, and it costs nothing to run.

```bash
# on the Pi
git clone https://github.com/jzewdu/ares-trading-bot.git ~/ares-trading-bot
cd ~/ares-trading-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt      # once requirements.txt exists
cp .env.example .env                 # then edit .env
```

Run it as a systemd service so a reboot brings it back:

```ini
# /etc/systemd/system/ares.service
[Unit]
Description=ARES trading loop
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/ares-trading-bot
EnvironmentFile=/home/pi/ares-trading-bot/.env
ExecStart=/home/pi/ares-trading-bot/.venv/bin/python bot.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ares
journalctl -u ares -f          # follow the loop's output
```

`Restart=always` is the point of the service: if the loop raises, it comes back
30s later instead of staying dead until you notice.

## Publishing code from the Pi

The bot source lives on the Pi; the repo lives on GitHub. `publish-from-pi.sh`
bridges them without hand-copying files.

**One-time auth** (needs write access to the repo) — pick one:

- **SSH (recommended):**
  ```bash
  ssh-keygen -t ed25519 -C "ares-pi"      # accept defaults
  cat ~/.ssh/id_ed25519.pub               # add at github.com/settings/keys
  export REPO_URL=git@github.com:jzewdu/ares-trading-bot.git
  ```
- **gh CLI:** `gh auth login` on the Pi, then keep the default HTTPS URL.

**Every time you want to publish:**

```bash
cd ~/ares                 # the folder with your .py files
~/publish-from-pi.sh      # add --dry-run to preview first
```

What it does: clones or fast-forwards `~/ares-trading-bot`, copies your bot
files in, refuses to stage anything that looks like a secret, commits, pushes.
It never deletes files in either folder.

**Manual fallback,** if you would rather do it by hand:

```bash
git clone https://github.com/jzewdu/ares-trading-bot.git ~/ares-trading-bot
cp ~/ares/*.py ~/ares-trading-bot/
cd ~/ares-trading-bot && git add -A && git commit -m "Add bot source"
git push
```
