#!/usr/bin/env bash
set -euo pipefail

# Install the learn-ai live engine as a systemd user service.
# The browser cannot start this host process, so normal operator setup should
# keep it running after login and let the UI treat /health as the truth source.

SERVICE_NAME="${SERVICE_NAME:-learn-ai-host-daemon.service}"
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PYTHON_EXE="${PYTHON_EXE:-$REPO_ROOT/PythonDataService/.venv/bin/python}"
PORT="${PORT:-8765}"
# Bind interface. Default loopback works on Windows/Mac podman, where the
# container reaches it via host.containers.internal -> host loopback (gvproxy).
# On LINUX rootless podman that alias maps to the bridge gateway, which does NOT
# reach loopback, so the container can't see a 127.0.0.1 daemon — set HOST=0.0.0.0
# there. Non-loopback is safe now that every protected route requires the
# X-Live-Runner-Token shared secret (ADR 0007); the token is auto-generated to
# artifacts/.host-daemon-token, which the container reads via the bind mount.
HOST="${HOST:-127.0.0.1}"
USER_SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$USER_SYSTEMD_DIR/$SERVICE_NAME"
WORKING_DIR="$REPO_ROOT/PythonDataService"
LIVE_RUNS_ROOT="$WORKING_DIR/artifacts/live_runs"
LOG_DIR="$WORKING_DIR/artifacts"

if [[ ! -x "$PYTHON_EXE" ]]; then
  echo "Python interpreter not found or not executable: $PYTHON_EXE" >&2
  echo "Set PYTHON_EXE=/path/to/python and re-run." >&2
  exit 1
fi

mkdir -p "$USER_SYSTEMD_DIR" "$LOG_DIR" "$LIVE_RUNS_ROOT"

cat > "$UNIT_PATH" <<UNIT
[Unit]
Description=learn-ai live engine
After=network.target

[Service]
Type=simple
WorkingDirectory=$WORKING_DIR
Environment=PYTHONPATH=$WORKING_DIR
ExecStart=$PYTHON_EXE -m app.engine.live.host_daemon --host $HOST --port $PORT --repo-root $REPO_ROOT --live-runs-root $LIVE_RUNS_ROOT
Restart=on-failure
RestartSec=10
StandardOutput=append:$LOG_DIR/host_daemon_service.out.log
StandardError=append:$LOG_DIR/host_daemon_service.err.log

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"

echo "Installed and started $SERVICE_NAME"
echo "Status: systemctl --user status $SERVICE_NAME"
echo "Logs:   $LOG_DIR/host_daemon_service.out.log / $LOG_DIR/host_daemon_service.err.log"
echo "If this machine should start it before login sessions, run: loginctl enable-linger \"$USER\""
