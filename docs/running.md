# Running ARES

ARES is a long-lived Python process with one job: stay up and run the 60-second
loop. It currently runs on a **Raspberry Pi 3 Model A+** at home, having moved
there from PythonAnywhere.

Everything below keeps the process alive, keeps the keys out of git, and keeps
it pointed at the paper endpoint.

## Configuration

Copy `.env.example` to `.env` and fill it in. Never commit `.env`.

| Variable | Purpose |
| --- | --- |
| `ALPACA_API_KEY` | Alpaca key id |
| `ALPACA_SECRET_KEY` | Alpaca secret |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` for paper trading |

Variable names in `.env.example` are the target convention — if the bot source
on the Pi uses different names, rename them there rather than in two places.

## The host: Raspberry Pi 3 Model A+

| Spec | Value | Why it matters here |
| --- | --- | --- |
| CPU | 1.4 GHz quad-core Cortex-A53 (BCM2837B0, 64-bit capable) | plenty for one loop every 60s |
| RAM | **512 MB** | the real constraint — see below |
| Network | dual-band 2.4/5 GHz Wi-Fi, **no Ethernet** | whole bot depends on Wi-Fi staying up |
| USB | **one** USB 2.0 port | keyboard or SSD, not both |
| Power | micro-USB, 5 V / 2.5 A | a weak supply is the #1 cause of mystery reboots |
| Clock | **no RTC** | time is wrong after every power cut until NTP syncs |
| Storage | microSD | finite write endurance |

### What that means in practice

- **512 MB of RAM.** Raspberry Pi OS Lite idles around 100 MB, which leaves
  ~400 MB for Python plus pandas/sklearn. That fits, but only if the Pi does
  nothing else: no desktop, no Jupyter, no second bot. Swap covers the peaks:

  ```bash
  sudo dphys-swapfile setup && sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=1024/' /etc/dphys-swapfile
  sudo dphys-swapfile swapon
  sudo apt install zram-tools    # compressed swap: kinder to the SD card
  ```

  If the OOM killer ever takes the process, `journalctl -k | grep -i oom` shows
  it — that is a memory problem, not a strategy problem.
- **Wi-Fi only, no Ethernet.** A dropped access point looks exactly like a
  quiet market. `Restart=always` handles the crash case, not a hung socket, so
  prefer 5 GHz if the router is close, and check `journalctl -u ares` for
  repeated connection errors before blaming the model.
- **No real-time clock.** After a power cut the clock can be hours off until
  NTP syncs, and anything that reasons about market hours will then reason
  about the wrong ones. Wait for a synced clock before trading:

  ```bash
  timedatectl show -p NTPSynchronized --value   # must be "yes"
  ```
- **microSD wear.** Logs and any SQLite file are write-heavy. Keep logs in the
  journal and cap it (below) rather than appending to a file on the card
  forever, and keep a copy of `.env` somewhere that is not the card.
- **Power.** Run `vcgencmd get_throttled` after any unexpected reboot; `0x0` is
  clean, anything else means undervoltage and the fix is a better supply, not
  more code.

## Phase 1 — Raspberry Pi 3 Model A+ (current)

```bash
sudo apt update && sudo apt install -y git python3-venv
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
After=network-online.target time-sync.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/ares-trading-bot
EnvironmentFile=/home/pi/ares-trading-bot/.env
ExecStart=/home/pi/ares-trading-bot/.venv/bin/python bot.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ares
journalctl -u ares -f          # follow the loop's output
```

`Restart=always` is the point of the service: if the loop raises, it comes back
30s later instead of staying dead until you notice. Cap the journal so the card
does not fill up:

```bash
sudo sed -i 's/^#\?SystemMaxUse=.*/SystemMaxUse=100M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
```

Python: use Raspberry Pi OS **Lite**. The 3 A+ is 64-bit capable, but at 512 MB
the 32-bit Lite image leaves noticeably more headroom — extra RAM beats extra
address space for a workload this small.

## Phase 2 — PythonAnywhere (earlier host, now fallback)

1. Upload the bot files (or `git clone` the repo).
2. Create the virtualenv and install the bot's requirements.
3. Set the three variables above in the **Web** or **Task** environment, or in
   a `.env` the process reads — not in the source.
4. Run it as an **Always-on task**: `python3 bot.py >> /tmp/ares.log 2>&1`
5. Confirm paper mode by checking that orders appear on the Alpaca paper
   dashboard, never the live one.

PythonAnywhere recycles always-on tasks. If the loop dies silently there, check
`/tmp/ares.log` before assuming a strategy bug. It remains useful as a
fallback host if the Pi is offline.

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
