# How v1 was built: Claude Code and Codex

Two agentic coding tools built ARES. This is the summary; the detail on the
right is what to fill in with your own prompts and turning points.

## The short version

| Phase | Tool | What it did |
| --- | --- | --- |
| v1, 2026-10-09 | Claude Code over Ollama | Wrote the bot: data pull, indicators, signal, order submission, risk limits |
| After v1 | Codex | Reviewed and refactored the loop |

## Why this split worked

- **Claude Code wrote the first version.** The task was greenfield and
  well-specified: "poll Alpaca every 60 seconds, compute RSI/MACD/SMA, size
  trades at 2% risk, stop at 5% daily loss." An agent with repository write
  access can turn that into a working loop in one sitting, because there is
  nothing existing to break.
- **Codex came second, on purpose.** Review is a different job from
  generation: it needs the code to already exist, and it works best on
  correctness and structure rather than features. Running the reviewer on the
  finished loop avoids two agents disagreeing over a half-written file.

## The loop, abstractly

This is the pattern both tools sat inside:

1. State the behaviour in plain English, including the numbers.
2. Let the agent write or change the code.
3. Run it against the paper account and read the log.
4. Feed the failure back as the next instruction.
5. Stop when the loop does what the description says — then commit.

The reason the risk limits are the most reliable part of the bot is that they
were stated as hard numbers in the prompt, not left to the model's judgement.

## Reconstructed, not recorded

The paragraphs above describe the shape of the work. The specifics — the exact
prompts, which model ran behind Ollama, what Codex changed in the refactor —
are not in the repo, so they are deliberately not invented here.

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
