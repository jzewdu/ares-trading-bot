# How ARES was built: Claude Code, Codex and Codebuff

Three AI coding tools touched this project, each doing a different job. This is
who did what, and what to fill in with your own prompts and turning points.

## Contributors at a glance

| Phase | Tool | Contribution |
| --- | --- | --- |
| v1, 2026-10-09 | Claude Code over Ollama | Wrote the bot: data pull, indicators, signal, order submission, risk limits |
| After v1 | Codex | Reviewed and refactored the loop |
| 2026-10-23 | Codebuff | Repo structure, documentation, Pi publish tooling — **no trading logic** |

## Phase 1 — Claude Code wrote v1

The task was greenfield and well-specified: "poll Alpaca every 60 seconds,
compute RSI/MACD/SMA, size trades at 2% risk, stop at 5% daily loss." An agent
with repository write access can turn that into a working loop in one sitting,
because there is nothing existing to break.

The risk limits are the most reliable part of the bot for a reason: they were
stated as hard numbers in the prompt rather than left to the model's judgement.

## Phase 2 — Codex reviewed and refactored

Codex came second, on purpose. Review is a different job from generation: it
needs the code to already exist, and it works best on correctness and structure
rather than features. Running the reviewer on the finished loop avoids two
agents disagreeing over a half-written file.

## Phase 3 — Codebuff organized the repository

By October 2026 the repo still held nothing but a README: the strategy, the
deployment and this history were undocumented, and the bot's actual source lived
only on the Pi with no route into git. Codebuff added:

| Added | Purpose |
| --- | --- |
| `README.md` (rewritten) | front page: what it is, the loop, hosts, repo map |
| `docs/how-it-works.md` | the 60s loop, indicators, 2% / 5% risk math, known gaps |
| `docs/running.md` | Pi 3 Model A+ constraints, systemd, PythonAnywhere fallback |
| `docs/build-loop.md` | this file |
| `.gitignore`, `.env.example` | keep secrets and runtime junk out of git |
| `scripts/publish-from-pi.sh` | publishes the Pi's source into this repo, refusing secrets |

**What it deliberately did not do:** read or modify the trading logic. The
source was not in the repo, so every strategy statement here comes from your
README and the deployment description — not from inspecting `bot.py`. Where the
docs say something the code alone could confirm (model type, indicator windows,
exact file names), they say so instead of guessing.

The publish script was tested against a throwaway local repo — first publish,
idempotent re-run, dry run, hardcoded-key refusal, secret-file refusal and
recovery — but not against the real Pi.

## The loop, abstractly

All three phases sat inside the same pattern:

1. State the behaviour in plain English, including the numbers.
2. Let the agent write or change the code.
3. Run it against the paper account and read the log.
4. Feed the failure back as the next instruction.
5. Stop when the loop does what the description says — then commit.

What changes between phases is only step 1: "build this" for Claude Code,
"review this" for Codex, "make this repo legible and get the source into it"
for Codebuff.

## Reconstructed, not recorded

The paragraphs above describe the shape of the work. The specifics — the exact
prompts, which model ran behind Ollama, what Codex changed — are not in the
repo, so they are deliberately not invented here.

To make this a real build log, append below:

- [ ] The opening prompt given to Claude Code.
- [ ] The model + Ollama version used locally.
- [ ] What Codex actually changed (diff summary or the review comment).
- [ ] The first bug the paper account caught.

## Why document this at all

Because "an AI wrote it" is not a review. A build log that names the tool, the
prompt and the fix is reproducible — someone else can re-run the same loop and
judge the result, which is the whole difference between a hobby bot and a
claim.
