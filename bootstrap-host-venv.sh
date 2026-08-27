#!/usr/bin/env bash
# Bootstrap the host Python venv at PythonDataService/.venv.
#
# Why this exists:
#   The full pytest suite and the operator CLIs documented under
#   docs/runbooks/ (e.g. alpaca-sqlite-clerk-recovery-and-cutover.md) run
#   from a host interpreter, not from the polygon-data-service container.
#   This script provisions that interpreter with the same requirement set
#   CI installs.
#
#   Until PR-B of #1813 (2026-08-27) this file was bootstrap-host-daemon.sh
#   and also supervised the IBKR host bridge (app.engine.live.host_daemon).
#   That bridge is retired; only the venv provisioning it carried survives.
#
# Usage:
#   ./bootstrap-host-venv.sh        # create the venv and install requirements
#
# Requirements are re-installed only when heavy/light/dev change — a stamp
# file records the hash of the three files together.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ $# -gt 0 ]]; then
  case "$1" in
    -h|--help)
      sed -n '2,19p' "$0"
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      echo "       Try --help." >&2
      exit 2
      ;;
  esac
fi

# ---------------------------------------------------------------------------
# 0. Sanity: this script is macOS-only (matches setup-macos.sh scope).
# ---------------------------------------------------------------------------
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: bootstrap-host-venv.sh is for macOS. On Linux/Windows, create" >&2
  echo "       PythonDataService/.venv with your platform's python3.12 and" >&2
  echo "       install requirements-heavy.txt + -light.txt + -dev.txt." >&2
  exit 1
fi

if [[ ! -d "$ROOT_DIR/PythonDataService" ]]; then
  echo "ERROR: PythonDataService/ not found in $ROOT_DIR — run from the repo root." >&2
  exit 1
fi

VENV_DIR="$ROOT_DIR/PythonDataService/.venv"

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
# 2. Venv at PythonDataService/.venv.
# ---------------------------------------------------------------------------
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "==> Creating venv at $VENV_DIR..."
  "$PYTHON312" -m venv "$VENV_DIR"
else
  echo "==> Venv exists at $VENV_DIR"
fi

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

echo ""
echo "==> Host venv ready: $VENV_DIR/bin/python"
