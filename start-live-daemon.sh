#!/usr/bin/env bash
# Start the host live-run daemon so the polygon-data-service container can reach
# it and Deploy / Start / Stop work from the UI.
#
# Why this script exists: the daemon's --host defaults to 127.0.0.1 (loopback
# only). The data-plane container reaches it via host.containers.internal, which
# resolves to the host gateway IP, NOT loopback -- so a loopback-bound daemon
# refuses the container's connection and Deploy fails with
#   "host daemon unreachable: All connection attempts failed" (503).
# Binding to 0.0.0.0 is the fix. This is NOT a market-hours issue.
#
# Usage:
#   ./start-live-daemon.sh              # run in foreground (Ctrl-C to stop)
#   ./start-live-daemon.sh --background # detach, log to /tmp/host_daemon.log
set -euo pipefail

# Repo root = directory this script lives in (so paths don't depend on cwd).
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$REPO/PythonDataService/.venv/bin/python"
PORT=8765
LOG=/tmp/host_daemon.log

if [[ ! -x "$PY" ]]; then
  echo "ERROR: venv python not found at $PY" >&2
  echo "       Create it / install deps under PythonDataService first." >&2
  exit 1
fi

# Stop any daemon already running (loopback-bound or otherwise).
if pkill -f "app.engine.live.host_daemon" 2>/dev/null; then
  echo "Stopped existing host daemon."
  sleep 1
fi

# PYTHONPATH must point at PythonDataService so `-m app...` resolves regardless
# of cwd or conda state. --repo-root must be the git root: the daemon appends
# "/PythonDataService" to it to set each live runner's PYTHONPATH.
export PYTHONPATH="$REPO/PythonDataService"
CMD=("$PY" -m app.engine.live.host_daemon --host 0.0.0.0 --repo-root "$REPO")

verify() {
  echo
  if ss -ltnp 2>/dev/null | grep -q "0.0.0.0:$PORT"; then
    echo "OK: daemon listening on 0.0.0.0:$PORT (container-reachable)."
  else
    echo "WARN: not listening on 0.0.0.0:$PORT yet -- check the log:" >&2
    echo "      tail -f $LOG" >&2
    return
  fi
  # Probe from inside the container (best-effort; needs polygon-data-service up).
  if command -v podman >/dev/null 2>&1; then
    code="$(podman exec polygon-data-service curl -s -m 3 \
      "http://host.containers.internal:$PORT/health" \
      -o /dev/null -w '%{http_code}' 2>/dev/null || echo 000)"
    if [[ "$code" == "200" ]]; then
      echo "OK: container probe http://host.containers.internal:$PORT/health -> 200."
      echo "    Deploy from the UI should now work."
    else
      echo "WARN: container probe returned HTTP $code (is polygon-data-service up?)." >&2
    fi
  fi
}

if [[ "${1:-}" == "--background" ]]; then
  setsid "${CMD[@]}" > "$LOG" 2>&1 < /dev/null &
  echo "Daemon started in background (pid $!), logging to $LOG."
  sleep 4
  verify
else
  echo "Starting host daemon in foreground (Ctrl-C to stop)..."
  echo "Bind: 0.0.0.0:$PORT   repo-root: $REPO"
  echo "(Run with --background to detach and free this terminal.)"
  echo
  exec "${CMD[@]}"
fi
