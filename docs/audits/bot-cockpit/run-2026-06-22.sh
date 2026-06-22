#!/bin/zsh
# Wrapper for the Bot Cockpit autonomous overnight run.
# Invoked by launchd at 2026-06-22 02:05 America/Chicago.
# Wall-clock cap is enforced inside the prompt itself (6h); this wrapper also
# applies a hard kill at 6h15m as a belt-and-braces safety net.

set -u

REPO=/Users/inkant/learn-ai
PROMPT_FILE="$REPO/docs/audits/bot-cockpit/run-prompt-2026-06-22.md"
LOG_FILE="$REPO/docs/audits/bot-cockpit/run-2026-06-22.log"
SUMMARY_FILE="$REPO/docs/audits/bot-cockpit/RUN-SUMMARY-2026-06-22.md"
CLAUDE_BIN=/opt/homebrew/bin/claude

mkdir -p "$REPO/docs/audits/bot-cockpit"

# Ensure PATH includes Homebrew (launchd ships a minimal PATH).
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Kick off the run inside caffeinate so the Mac doesn't sleep, and tee everything.
{
  echo "============================================================"
  echo "Bot Cockpit autonomous run starting"
  echo "Wrapper start: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "Repo: $REPO"
  echo "Prompt: $PROMPT_FILE"
  echo "Model: claude-opus-4-7"
  echo "============================================================"
  echo

  cd "$REPO" || {
    echo "FATAL: cannot cd to $REPO"
    exit 2
  }

  # Hard kill after 6h15m as the outer safety net (the prompt enforces 6h itself).
  /usr/bin/caffeinate -i \
    /usr/bin/timeout --signal=TERM --kill-after=60 22500 \
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
} 2>&1 | /usr/bin/tee -a "$LOG_FILE"
