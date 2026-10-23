#!/usr/bin/env bash
#
# publish-from-pi.sh — publish the ARES bot files into the GitHub repo.
#
# Run this ON THE RASPBERRY PI, from the folder that holds the bot's .py files:
#
#     cd ~/ares
#     ~/publish-from-pi.sh              # publish
#     ~/publish-from-pi.sh --dry-run   # show what would be copied, change nothing
#
# It clones (or updates) the repo into $CLONE_DIR, copies your files in,
# refuses to stage anything that looks like a secret, commits and pushes.
# It never deletes files, in either folder.
#
# One-time auth (write access required), pick one:
#   SSH:  ssh-keygen -t ed25519 -C ares-pi  →  add ~/.ssh/id_ed25519.pub at
#         github.com/settings/keys
#         export REPO_URL=git@github.com:jzewdu/ares-trading-bot.git
#   gh:   gh auth login   (then the default HTTPS URL works)
#
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/jzewdu/ares-trading-bot.git}"
CLONE_DIR="${CLONE_DIR:-$HOME/ares-trading-bot}"
DRY_RUN=0
SRC_DIR=""

usage() { sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *)  SRC_DIR="$1" ;;
  esac
  shift
done

SRC_DIR="${SRC_DIR:-$PWD}"
say() { printf '  %s\n' "$*"; }
die() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

# Files the repo owns. If the bot folder has its own copies, they are skipped
# so the repo's README and docs are not overwritten.
EXCLUDES=(
  .git .env .env.* .venv venv env node_modules __pycache__
  '*.py[cod]' '*.log' '*.pem' '*.key' '*.sqlite' '*.db'
  logs data models .pytest_cache .DS_Store
  README.md docs scripts .gitignore .env.example
)

echo "ARES publish"
say "source:   $SRC_DIR"
say "repo:     $REPO_URL"
say "checkout: $CLONE_DIR"
[ "$DRY_RUN" = 1 ] && say "mode:     dry run (nothing is written or pushed)"

# ── checks ───────────────────────────────────────────────────────────────────
[ -d "$SRC_DIR" ] || die "source folder not found: $SRC_DIR"
command -v git >/dev/null || die "git is not installed"
[ "$(cd "$SRC_DIR" && pwd -P)" = "$(cd "$CLONE_DIR" 2>/dev/null && pwd -P || echo -)" ] \
  && die "source and checkout are the same folder; run this from the bot folder"

py_count=$(find "$SRC_DIR" -maxdepth 2 -name '*.py' -not -path '*/.*' | wc -l | tr -d ' ')
[ "$py_count" -eq 0 ] && echo "warning: no .py files found in $SRC_DIR — is this the bot folder?"

# Hardcoded keys are the failure this whole script exists to prevent, so check
# before anything is copied or committed.
hardcoded=$(grep -rInE "(ALPACA_SECRET_KEY|ALPACA_API_KEY|APCA_API_SECRET_KEY)[[:space:]]*[:=][[:space:]]*['\"][^'\"]{8,}" \
  --exclude-dir=.git --exclude-dir=.venv --exclude-dir=venv --exclude-dir=__pycache__ \
  --exclude='.env*' "$SRC_DIR" || true)
if [ -n "$hardcoded" ]; then
  printf '\nrefusing to publish — keys appear hardcoded in the source:\n%s\n\n' "$hardcoded" >&2
  die "read them from the environment instead (see .env.example)"
fi

# ── checkout ─────────────────────────────────────────────────────────────────
if [ -d "$CLONE_DIR/.git" ]; then
  say "updating existing checkout"
  if [ -n "$(git -C "$CLONE_DIR" status --porcelain)" ]; then
    die "uncommitted changes in $CLONE_DIR — commit or stash them there first"
  fi
  [ "$DRY_RUN" = 0 ] && git -C "$CLONE_DIR" pull --ff-only --quiet
else
  say "cloning"
  [ "$DRY_RUN" = 0 ] && git clone --quiet "$REPO_URL" "$CLONE_DIR"
fi

# ── copy ─────────────────────────────────────────────────────────────────────
args=()
for pat in "${EXCLUDES[@]}"; do args+=(--exclude "$pat"); done

if command -v rsync >/dev/null; then
  [ "$DRY_RUN" = 1 ] && args+=(--dry-run --itemize-changes)
  rsync -a "${args[@]}" "$SRC_DIR"/ "$CLONE_DIR"/
else
  say "rsync not installed; using tar"
  if [ "$DRY_RUN" = 1 ]; then
    say "(dry run with tar is not itemised — install rsync for a preview)"
  else
    tar_args=()
    for pat in "${EXCLUDES[@]}"; do tar_args+=(--exclude="$pat"); done
    ( cd "$SRC_DIR" && tar cf - "${tar_args[@]}" . ) | ( cd "$CLONE_DIR" && tar xf - )
  fi
fi
say "copied bot files"

[ "$DRY_RUN" = 1 ] && { echo; echo "dry run complete — nothing changed."; exit 0; }

# ── secret guard ─────────────────────────────────────────────────────────────
leaks=$(cd "$CLONE_DIR" && git status --porcelain -uall | awk '{print $2}' \
  | grep -Ei '(^|/)(\.env($|\.)|.*\.pem|.*\.key|id_rsa.*|id_ed25519.*|credentials.*\.json)$' || true)
if [ -n "$leaks" ]; then
  # Leave the checkout clean: these files stay untouched in $SRC_DIR.
  git -C "$CLONE_DIR" checkout -q -- . 2>/dev/null || true
  git -C "$CLONE_DIR" clean -fdq 2>/dev/null || true
  printf '\nrefusing to publish — these look like secrets:\n%s\n\n' "$leaks" >&2
  die "they were skipped, not copied — rename or ignore them in $SRC_DIR"
fi

# ── commit & push ────────────────────────────────────────────────────────────
cd "$CLONE_DIR"
git add -A
if git diff --cached --quiet; then
  echo "nothing to publish — repo already matches $SRC_DIR"
  exit 0
fi

git -c user.name="${GIT_AUTHOR_NAME:-$(git config user.name || echo ares-pi)}" \
    -c user.email="${GIT_AUTHOR_EMAIL:-$(git config user.email || echo ares-pi@localhost)}" \
    commit --quiet -m "Publish bot source from $(hostname -s) ($(date +%Y-%m-%d))"

say "committed $(git rev-parse --short HEAD)"
if git push --quiet origin HEAD:main; then
  echo "pushed to $REPO_URL (main)"
else
  die "push failed — set up auth (see the header of this script) and run again"
fi
