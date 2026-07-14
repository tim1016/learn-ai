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
#   ./start-live-daemon.sh --env-file .env --background
#   ./start-live-daemon.sh --print-launch-env
set -euo pipefail

# Repo root = directory this script lives in (so paths don't depend on cwd).
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON_EXE:-$REPO/PythonDataService/.venv/bin/python}"
PORT=8765
LOG=/tmp/host_daemon.log
ENV_FILE="${LEARN_AI_DAEMON_ENV_FILE:-$REPO/.env}"
MODE=foreground

usage() {
  sed -n '2,16p' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --background)
      MODE=background
      shift
      ;;
    --foreground)
      MODE=foreground
      shift
      ;;
    --env-file)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --env-file requires a path." >&2
        exit 2
      fi
      ENV_FILE="$2"
      shift 2
      ;;
    --print-launch-env)
      MODE=print-launch-env
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      echo "       Try --help." >&2
      exit 2
      ;;
  esac
done

if [[ "$ENV_FILE" != /* ]]; then
  ENV_FILE="$REPO/$ENV_FILE"
fi

if [[ ! -x "$PY" ]]; then
  echo "ERROR: venv python not found at $PY" >&2
  echo "       Create it / install deps under PythonDataService first, or set PYTHON_EXE." >&2
  exit 1
fi

# PYTHONPATH must point at PythonDataService so `-m app...` resolves regardless
# of cwd or conda state. --repo-root must be the git root: the daemon appends
# "/PythonDataService" to it to set each live runner's PYTHONPATH.
export PYTHONPATH="$REPO/PythonDataService"
CMD=("$PY" -m app.engine.live.host_daemon --host 0.0.0.0 --repo-root "$REPO" --env-file "$ENV_FILE")

print_launch_env() {
  "$PY" - "$ENV_FILE" <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from app.engine.live.host_runner_policy import allowed_ibkr_hosts, load_policy_env_file

effective_env = dict(os.environ)
load_policy_env_file(Path(sys.argv[1]), environ=effective_env)
payload = {
    "IBKR_HOST": effective_env.get("IBKR_HOST", ""),
    "IBKR_HOST_ALLOWLIST": effective_env.get("IBKR_HOST_ALLOWLIST", ""),
    "LIVE_RUNNER_IBKR_CLIENT_ID_POOL": effective_env.get("LIVE_RUNNER_IBKR_CLIENT_ID_POOL", ""),
    "allowed_ibkr_hosts": sorted(allowed_ibkr_hosts(effective_env)),
    "env_file": str(Path(sys.argv[1])),
}
sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
PY
}

if [[ "$MODE" == "print-launch-env" ]]; then
  print_launch_env
  exit 0
fi

# Stop any daemon already running (loopback-bound or otherwise).
if pkill -f "app.engine.live.host_daemon" 2>/dev/null; then
  echo "Stopped existing host daemon."
  sleep 1
fi

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

if [[ "$MODE" == "background" ]]; then
  if command -v setsid >/dev/null 2>&1; then
    setsid "${CMD[@]}" > "$LOG" 2>&1 < /dev/null &
    daemon_pid=$!
  else
    daemon_pid="$(
      "$PY" - "$LOG" "${CMD[@]}" <<'PY'
from __future__ import annotations

import subprocess
import sys

log_path = sys.argv[1]
cmd = sys.argv[2:]
log = open(log_path, "ab", buffering=0)
proc = subprocess.Popen(
    cmd,
    stdin=subprocess.DEVNULL,
    stdout=log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
    close_fds=True,
)
print(proc.pid)
PY
    )"
  fi
  echo "Daemon started in background (pid $daemon_pid), logging to $LOG."
  sleep 4
  verify
else
  echo "Starting host daemon in foreground (Ctrl-C to stop)..."
  echo "Bind: 0.0.0.0:$PORT   repo-root: $REPO"
  echo "Env file: $ENV_FILE (IBKR_HOST_ALLOWLIST / IBKR_HOST only; process env wins)"
  echo "(Run with --background to detach and free this terminal.)"
  echo
  exec "${CMD[@]}"
fi
