#!/bin/zsh
# Wrapper for the Bot Cockpit autonomous overnight run.
# Invoked by launchd at 2026-06-22 02:05 America/Chicago.
# Wall-clock cap is enforced inside the prompt itself (6h); this wrapper also
# applies a hard kill at 6h15m as a belt-and-braces safety net IF a timeout
# binary is available (macOS does not ship one by default — see CR-2 below).

set -u
set -o pipefail   # so the final `| tee` doesn't mask claude's exit code

REPO=/Users/inkant/learn-ai
PROMPT_FILE="$REPO/docs/audits/bot-cockpit/run-prompt-2026-06-22.md"
LOG_FILE="$REPO/docs/audits/bot-cockpit/run-2026-06-22.log"
SUMMARY_FILE="$REPO/docs/audits/bot-cockpit/RUN-SUMMARY-2026-06-22.md"
CLAUDE_BIN=/opt/homebrew/bin/claude

mkdir -p "$REPO/docs/audits/bot-cockpit"

# Ensure PATH includes Homebrew (launchd ships a minimal PATH).
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# CR-2 — Pick an available outer-timeout wrapper.  macOS does not ship
# /usr/bin/timeout; coreutils provides ``gtimeout`` after ``brew install
# coreutils``.  If neither is available we drop the outer layer entirely
# — the prompt's internal 6h cap is the primary enforcement and an
# absent outer net is preferable to an exit 127 before claude starts.
# The previous wrapper hard-coded /usr/bin/timeout and the 02:05 CT
# 2026-06-22 run died at exit 127 (no such file).
if command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_PREFIX=(gtimeout --signal=TERM --kill-after=60 22500)
elif command -v timeout >/dev/null 2>&1; then
  TIMEOUT_PREFIX=(timeout --signal=TERM --kill-after=60 22500)
else
  TIMEOUT_PREFIX=()
fi

# All output tee'd into LOG_FILE; RC captures claude's exit code; the
# block's final ``exit $RC`` (after the pipe) propagates that to
# launchd via pipefail.  Pre-fix the trailing ``| tee`` ate the real
# exit code and launchd saw success on failure.
{
  echo "============================================================"
  echo "Bot Cockpit autonomous run starting"
  echo "Wrapper start: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "Repo: $REPO"
  echo "Prompt: $PROMPT_FILE"
  echo "Model: claude-opus-4-7"
  if (( ${#TIMEOUT_PREFIX[@]} )); then
    echo "Outer timeout: ${TIMEOUT_PREFIX[*]}"
  else
    echo "Outer timeout: <none available> — relying on prompt-internal 6h cap"
  fi
  echo "============================================================"
  echo

  cd "$REPO" || {
    echo "FATAL: cannot cd to $REPO"
    exit 2
  }

  /usr/bin/caffeinate -i \
    "${TIMEOUT_PREFIX[@]}" \
    "$CLAUDE_BIN" \
      -p \
      --model claude-opus-4-7 \
      --dangerously-skip-permissions \
      --add-dir "$REPO" \
      "$(cat "$PROMPT_FILE")"

  RC=$?
  echo
  echo "============================================================"
  echo "Wrapper end:   $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "Claude exit code: $RC"
  echo "============================================================"

  # Read the agent's STATUS line for the macOS notification, if it exists.
  if [[ -f "$SUMMARY_FILE" ]]; then
    STATUS_LINE=$(head -1 "$SUMMARY_FILE")
  else
    STATUS_LINE="STATUS: NO SUMMARY WRITTEN (exit $RC)"
  fi

  /usr/bin/osascript -e "display notification \"$STATUS_LINE\" with title \"Bot Cockpit Audit\""

  # Self-unload so this one-shot does not fire again next 6/22.
  PLIST=$HOME/Library/LaunchAgents/com.inkant.bot-cockpit-audit.plist
  if [[ -f "$PLIST" ]]; then
    /bin/launchctl unload "$PLIST" 2>/dev/null || true
    /bin/mv "$PLIST" "$PLIST.fired-$(date '+%Y%m%d-%H%M%S')" 2>/dev/null || true
  fi

  exit $RC
} 2>&1 | /usr/bin/tee -a "$LOG_FILE"
# pipefail above carries the exit status of the LEFT side of the pipe
# (the curly-brace block) through to the script's exit status.
