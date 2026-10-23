# How ARES was built

## Timeline

| When | What happened |
| --- | --- |
| 2026-10-09 | **v1** — stock bot: RSI/MACD/SMA, 60-second poll, 2% risk per trade, 5% daily stop. Written with Claude Code over Ollama, hosted on PythonAnywhere. |
| After v1 | **Codex** reviewed and refactored the v1 loop. |
| 2026-10-18 | **v2** — the code in this repo: paper-only Alpaca crypto breakout bot, 300-second cycle, 15-minute bars, Flask dashboard, SQLite state, systemd unit. The previous project contents were cleared at the user's direction and rebuilt from a written [rollout plan](rollout-plan.md). |
| After that | Deployed to the **Raspberry Pi 3 Model A+**, dashboard reached over an SSH tunnel. |

Dates are the bot's milestones. The documentation and repository tooling in
this repo were added later, in a separate pass, and are not part of this
timeline.

## Phase 1 — Claude Code wrote v1

The task was greenfield and fully specified: poll Alpaca, compute indicators,
size trades, enforce daily limits. An agent with write access can turn that into
a working loop in one sitting, because there is nothing existing to break.

v1's risk limits were stated as hard numbers in the prompt rather than left to
the model's judgement — a large part of why they survived contact with the
market while strategy details changed.

## Phase 2 — Codex reviewed it

Codex came second, on purpose. Review is a different job from generation: it
needs the code to already exist and works best on correctness and structure
rather than features. Running the reviewer on the finished loop avoids two
agents disagreeing over a half-written file.

## Phase 3 — the v2 rewrite (2026-10-18)

v2 is not v1 with patches; it is a different bot. The evidence is in the repo:

| v1 | v2 |
| --- | --- |
| stocks | crypto (Alpaca USD/USDC pairs) |
| RSI + MACD + SMA, model-fed | 20-bar breakout + 20/50 SMA + volume |
| 60-second poll | 300-second poll |
| 2% risk per trade, 5% daily stop | 3% per position, 15% exposure, 3% daily stop, 5 positions |
| no dashboard | authenticated Flask dashboard with pause/resume |
| PythonAnywhere | Raspberry Pi 3 Model A+ |

The v2 work began with the plan now kept at
[docs/rollout-plan.md](rollout-plan.md) — safety defaults first (paper only, no
live host in the client, localhost-only dashboard, explicit caps), then
`alpaca_client.py`, `strategy.py`, `engine.py`, `state.py` and `app.py`. The
plan's own deployment section records that v2 had not yet been installed on the
Pi when it was written; that came afterwards.

## Phase 4 — documentation (this pass)

The repository held a description of v1 and none of the actual code. This pass
copied v2 in, wrote the docs in `docs/` from the source (not from the older
description), and added the Pi publish script. It is not part of the bot's
history: nothing in `engine.py`, `strategy.py` or `config.py` was changed.

Two things are worth stating because the previous docs got them wrong: the bot
does not use RSI/MACD or a model, and it polls every 300 seconds, not 60.

## Reconstructed, not recorded

The specifics below are not recoverable from the repo, so they are left blank
rather than invented:

- [ ] The opening prompt given to Claude Code for v1.
- [ ] The model and Ollama version used locally.
- [ ] What Codex actually changed in the v1 refactor.
- [ ] Why the strategy moved from stock indicators to a crypto breakout.
- [ ] The date the Pi went live, and the first paper-account bug it caught.

## Why document this at all

Because "an AI wrote it" is not a review. A build log that names the tool, the
prompt and the fix is reproducible — someone else can re-run the same loop and
judge the result, which is the whole difference between a hobby bot and a
claim.
