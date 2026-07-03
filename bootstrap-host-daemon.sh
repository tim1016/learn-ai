#!/usr/bin/env bash
# Bootstrap the host Python venv and run the live-engine host daemon.
#
# Why this exists:
#   The UI surfaces under /broker/* poll a HOST process at 127.0.0.1:8765
#   for "Live engine" reachability and to actuate run start/stop. The daemon
#   cannot live in a container because IBKR Gateway binds reqRealTimeBars to
#   the login-session source IP (error 420 — same-IP binding); a
#   polygon-data-service-IP client is rejected. setup-macos.sh provisions the
#   container stack but not this host venv, so on a freshly-bootstrapped
#   machine /broker/instances shows "Live engine unavailable" with no working
#   Recheck. This script closes that gap.
#
# Usage:
#   ./bootstrap-host-daemon.sh                 # ensure venv exists, then start (default)
#   ./bootstrap-host-daemon.sh --setup-only    # venv + pip install, no daemon launch
#   ./bootstrap-host-daemon.sh --restart       # pkill running daemon, then start
#   ./bootstrap-host-daemon.sh --stop          # graceful stop: refuse if managed runs are live
#   ./bootstrap-host-daemon.sh --stop --force  # stop daemon even if managed runs are still live
#                                             # (those runners will be orphaned — see Codex P1)
#   ./bootstrap-host-daemon.sh --status        # report whether daemon is up
#
# Override the daemon port with HOST_DAEMON_PORT (default 8765 — matches
# Frontend's environment.liveRunnerDaemonUrl).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# ---------------------------------------------------------------------------
# 0. Sanity: this script is macOS-only (matches setup-macos.sh scope).
# ---------------------------------------------------------------------------
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: bootstrap-host-daemon.sh is for macOS. On Linux/Windows, follow" >&2
  echo "       docs/runbooks/ibkr-paper-dry-run.md to set up the host venv." >&2
  exit 1
fi

if [[ ! -d "$ROOT_DIR/PythonDataService" ]]; then
  echo "ERROR: PythonDataService/ not found in $ROOT_DIR — run from the repo root." >&2
  exit 1
fi

VENV_DIR="$ROOT_DIR/PythonDataService/.venv"
ARTIFACTS_DIR="$ROOT_DIR/PythonDataService/artifacts"
LOG_FILE="$ARTIFACTS_DIR/host_daemon.log"
PID_FILE="$ARTIFACTS_DIR/host_daemon.pid"
PORT="${HOST_DAEMON_PORT:-8765}"
HEALTH_URL="http://127.0.0.1:${PORT}/health"
# Repo-scoped pgrep pattern: the daemon's argv carries `--repo-root $ROOT_DIR`,
# so this match only finds the daemon launched for THIS checkout. Without the
# scope, a second checkout's daemon (or any other process whose argv happens to
# contain `app.engine.live.host_daemon`) would be pgrep'd / pkill'd by mistake.
DAEMON_MATCH="app.engine.live.host_daemon --repo-root $ROOT_DIR"
TOKEN_FILE="$ROOT_DIR/PythonDataService/artifacts/.host-daemon-token"

MODE="start"
FORCE=false
for arg in "$@"; do
  case "$arg" in
    --start)        MODE="start" ;;
    --setup-only)   MODE="setup-only" ;;
    --restart)      MODE="restart" ;;
    --stop)         MODE="stop" ;;
    --status)       MODE="status" ;;
    --force)        FORCE=true ;;
    -h|--help)
      sed -n '2,23p' "$0"
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $arg" >&2
      echo "       Try --help." >&2
      exit 2
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
daemon_running() {
  pgrep -f "$DAEMON_MATCH" >/dev/null 2>&1
}

# List run_ids of managed runs whose process is still alive. The daemon launches
# each runner with start_new_session=True (host_daemon.py), so the runner is its
# OWN session leader and survives an unconditional pkill of the daemon — Codex
# P1 on the first review. We query /instances (with the shared-secret token) and
# return the run_ids whose process.state is not a terminal state. If the daemon
# is unreachable, or the token file is absent, return "unknown" so the caller
# can warn rather than silently assume "no active runs".
active_run_ids() {
  if ! curl -fsS -o /dev/null "$HEALTH_URL" 2>/dev/null; then
    echo "unknown"
    return 0
  fi
  if [[ ! -r "$TOKEN_FILE" ]]; then
    echo "unknown"
    return 0
  fi
  local token instances
  token="$(cat "$TOKEN_FILE")"
  if ! instances="$(curl -fsS -H "X-Live-Runner-Token: $token" "http://127.0.0.1:${PORT}/instances" 2>/dev/null)"; then
    echo "unknown"
    return 0
  fi
  # parse JSON: emit one run_id per line for non-terminal process states.
  # 'idle' / 'exited' / 'failed' are terminal; anything else (running, stopping,
  # ...) means the runner is still alive. System python3 (>=3.7) is sufficient
  # — the stop path may run before the venv is created.
  printf '%s' "$instances" | python3 -c '
import json, sys
data = json.load(sys.stdin)
terminal = {"idle", "exited", "failed"}
for inst in data.get("instances", []):
    state = (inst.get("process") or {}).get("state")
    if state and state not in terminal:
        print(inst.get("run_id"))
'
}

stop_daemon() {
  if ! daemon_running; then
    echo "==> No host daemon process to stop."
    rm -f "$PID_FILE"
    return 0
  fi
  # Before pkill: refuse to kill the daemon while it manages live runners.
  # `start_new_session=True` decouples them from the daemon's process group,
  # so a bare pkill orphans them — the UI loses its only control path while a
  # paper/live runner can keep trading. --force overrides for cases where the
  # operator has already stopped the runner some other way.
  local actives
  actives="$(active_run_ids || true)"
  if [[ "$actives" == "unknown" ]]; then
    if ! $FORCE; then
      echo "ERROR: Daemon is running but /instances is unreachable — cannot tell" >&2
      echo "       whether managed runners are still live. Refusing to stop." >&2
      echo "       Re-run with --force to stop anyway (any live runner will be orphaned)," >&2
      echo "       or run --status to inspect, or restart the daemon and retry." >&2
      return 1
    fi
    echo "==> /instances unreachable; --force given, proceeding."
  elif [[ -n "$actives" ]]; then
    if ! $FORCE; then
      echo "ERROR: Daemon is still managing live runners; refusing to stop." >&2
      echo "       Stop each run via the UI (or POST /runs/<id>/stop on the daemon)" >&2
      echo "       before halting the daemon, OR re-run with --force to abandon" >&2
      echo "       them (they will continue running and be orphaned)." >&2
      echo "       Active runs:" >&2
      echo "$actives" | sed 's/^/         /' >&2
      return 1
    fi
    echo "==> Daemon has active runs; --force given, proceeding (runners will be orphaned):"
    echo "$actives" | sed 's/^/    /'
  fi
  echo "==> Stopping running host daemon (pkill -f $DAEMON_MATCH)..."
  pkill -f "$DAEMON_MATCH" || true
  # Wait up to 5s for graceful exit before SIGKILL.
  for _ in 1 2 3 4 5; do
    daemon_running || break
    sleep 1
  done
  if daemon_running; then
    echo "==> Daemon did not exit on SIGTERM; sending SIGKILL."
    pkill -9 -f "$DAEMON_MATCH" || true
  fi
  rm -f "$PID_FILE"
}

report_status() {
  if daemon_running; then
    local pid
    pid="$(pgrep -f "$DAEMON_MATCH" | head -1)"
    if curl -fsS -o /dev/null "$HEALTH_URL" 2>/dev/null; then
      echo "    ✅ Daemon running (pid $pid) — $HEALTH_URL responding."
    else
      echo "    ⚠️  Daemon process exists (pid $pid) but $HEALTH_URL is not responding."
      echo "        Tail of $LOG_FILE:"
      [[ -f "$LOG_FILE" ]] && tail -10 "$LOG_FILE" | sed 's/^/        | /'
    fi
  else
    echo "    ⛔ No daemon process; $HEALTH_URL is down."
  fi
}

# Short-circuit modes that don't need the venv.
case "$MODE" in
  stop)
    stop_daemon
    exit 0
    ;;
  status)
    report_status
    exit 0
    ;;
esac

# ---------------------------------------------------------------------------
# 1. Homebrew + Python 3.12 (matches the container image's interpreter).
# ---------------------------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
  echo "ERROR: Homebrew not found. Install it first:" >&2
  echo '       /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' >&2
  exit 1
fi

if ! command -v /opt/homebrew/bin/python3.12 >/dev/null 2>&1; then
  echo "==> Installing python@3.12 via Homebrew..."
  brew install python@3.12
else
  echo "==> python@3.12 already installed: $(/opt/homebrew/bin/python3.12 --version)"
fi

PYTHON312="/opt/homebrew/bin/python3.12"

# ---------------------------------------------------------------------------
# 2. Venv at PythonDataService/.venv (matches docs/runbooks/ibkr-paper-dry-run.md).
# ---------------------------------------------------------------------------
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "==> Creating venv at $VENV_DIR..."
  "$PYTHON312" -m venv "$VENV_DIR"
else
  echo "==> Venv exists at $VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# ---------------------------------------------------------------------------
# 3. pip install — heavy + light + dev (same set CI installs; see
#    .claude/rules/python.md "Adding a Python dependency").
#
#    Skip if a stamp file shows the requirements have not changed since the
#    last successful install. Hash the three files together; any edit
#    invalidates the stamp and re-installs.
# ---------------------------------------------------------------------------
REQS=(
  "$ROOT_DIR/PythonDataService/requirements-heavy.txt"
  "$ROOT_DIR/PythonDataService/requirements-light.txt"
  "$ROOT_DIR/PythonDataService/requirements-dev.txt"
)
STAMP_FILE="$VENV_DIR/.bootstrap-reqs.sha"
CURRENT_HASH="$(cat "${REQS[@]}" | shasum -a 256 | awk '{print $1}')"

if [[ -f "$STAMP_FILE" ]] && [[ "$(cat "$STAMP_FILE")" == "$CURRENT_HASH" ]]; then
  echo "==> Requirements unchanged since last install — skipping pip install."
else
  echo "==> Installing pip requirements (heavy + light + dev, first run is slow)..."
  "$VENV_PIP" install --upgrade pip >/dev/null
  "$VENV_PIP" install -r "${REQS[0]}" -r "${REQS[1]}" -r "${REQS[2]}"
  echo "$CURRENT_HASH" > "$STAMP_FILE"
  echo "==> Requirements installed; stamp $(echo "$CURRENT_HASH" | head -c 12)... saved."
fi

if [[ "$MODE" == "setup-only" ]]; then
  echo ""
  echo "==> Setup complete. To start the daemon: ./bootstrap-host-daemon.sh"
  exit 0
fi

# ---------------------------------------------------------------------------
# 4. Restart path: stop any running daemon before launching a fresh one.
# ---------------------------------------------------------------------------
if [[ "$MODE" == "restart" ]]; then
  stop_daemon
fi

# ---------------------------------------------------------------------------
# 5. Launch the daemon in the background, nohup-detached so the script can
#    exit cleanly and the daemon survives the shell.
# ---------------------------------------------------------------------------
if daemon_running; then
  # An existing process whose health endpoint is also responding is the
  # happy case — print status and exit 0. But if the process exists and
  # /health is not responding, the daemon is stuck; reporting "already
  # running" + exit 0 lies to the caller (CodeRabbit). Exit non-zero so
  # setup-macos.sh fails loudly and the operator can --restart.
  if curl -fsS -o /dev/null "$HEALTH_URL" 2>/dev/null; then
    echo "==> Daemon is already running. Use --restart to relaunch, or --stop to halt."
    report_status
    exit 0
  fi
  echo "ERROR: Daemon process exists but $HEALTH_URL is not responding." >&2
  echo "       Run ./bootstrap-host-daemon.sh --restart to recover." >&2
  report_status >&2
  exit 1
fi

if curl -fsS -o /dev/null "$HEALTH_URL" 2>/dev/null; then
  echo "ERROR: $HEALTH_URL is responding but no matching daemon process was found." >&2
  echo "       Something else is bound to port $PORT. Free the port and retry." >&2
  exit 1
fi

mkdir -p "$ARTIFACTS_DIR"
echo "==> Starting host daemon on port $PORT (log: $LOG_FILE)..."
# nohup + setsid keeps the daemon alive after this script exits.
nohup env PYTHONPATH="$ROOT_DIR/PythonDataService" \
  "$VENV_PYTHON" -m app.engine.live.host_daemon \
  --repo-root "$ROOT_DIR" \
  --port "$PORT" \
  --env-file "$ROOT_DIR/.env" \
  > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

# ---------------------------------------------------------------------------
# 6. Wait for /health — like setup-macos.sh's wait_for. Failure prints the
#    daemon log tail so the cause is visible without a second command.
# ---------------------------------------------------------------------------
tries=30
while (( tries-- > 0 )); do
  if ! daemon_running; then
    echo "    ❌ Daemon process exited before /health came up." >&2
    echo "       Tail of $LOG_FILE:" >&2
    [[ -f "$LOG_FILE" ]] && tail -20 "$LOG_FILE" | sed 's/^/       | /' >&2
    rm -f "$PID_FILE"
    exit 1
  fi
  if curl -fsS -o /dev/null "$HEALTH_URL" 2>/dev/null; then
    echo "    ✅ Daemon up at $HEALTH_URL (pid $(cat "$PID_FILE"))."
    echo ""
    echo "    Stop with:  ./bootstrap-host-daemon.sh --stop"
    echo "    Tail log:   tail -f $LOG_FILE"
    exit 0
  fi
  sleep 1
done

echo "    ⚠️  Daemon did not answer $HEALTH_URL within 30s — see $LOG_FILE." >&2
exit 1
