"""Shared-secret auth between the data plane and the host live-run daemon.

The daemon exposes ``/deploy`` (mints a run after a git clean-tree check),
``/runs/{id}/start`` and ``/runs/{id}/stop`` (spawn and signal live-trading
subprocesses): anyone who can reach the daemon's port can deploy and run
strategies against a brokerage account. Loopback-only binding used to be the
only thing protecting that capability (``host_daemon`` rejected any non-loopback
``--host``). But the containerized data plane reaches the daemon via
``host.containers.internal``, which forwards to host loopback on Windows/Mac
podman (gvproxy) yet maps to the bridge gateway on Linux rootless podman — so on
Linux the loopback-only daemon is simply unreachable and the whole live UI is
dead in-container (ADR 0007).

This module makes auth mandatory so the daemon can safely bind a non-loopback
interface: the daemon generates a token at startup (if env is unset) and writes
it to a shared bind-mounted file the data plane reads. Operators who want to set
their own token use ``LIVE_RUNNER_DAEMON_TOKEN`` on both processes; the
file-backed fallback only activates when neither side has an env override.

Deliberately self-contained — it mirrors ``app.lean_sidecar.launcher_auth`` but
does NOT import it: the host daemon must not depend on the lean-sidecar
subsystem. The two host services keep parallel, independent token files.

Wire format: ``X-Live-Runner-Token: <token>`` header on every protected request.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Env var (operator override) checked first on both the daemon and the data
# plane. When set on both sides, no token file is read or written.
TOKEN_ENV_VAR = "LIVE_RUNNER_DAEMON_TOKEN"

# Header carrying the token on every protected request.
TOKEN_HEADER = "X-Live-Runner-Token"

# File the daemon writes the auto-generated token to + the data plane reads it
# from. Lives at the artifacts-root level (sibling to live_runs/) so both
# processes view the same bytes through the shared bind mount
# (``./PythonDataService/artifacts:/app/artifacts``). Hidden prefix so a listing
# of artifacts doesn't surface it as a run.
TOKEN_FILENAME = ".host-daemon-token"

# 32 bytes URL-safe base64 — ~256 bits of entropy, matching the launcher token.
_TOKEN_BYTES = 32

# Owner read/write only. The data-plane container user reads it through the bind
# mount; other host users on a shared workstation must not.
_TOKEN_FILE_MODE = 0o600


def token_file_path(artifacts_root: Path) -> Path:
    """Resolve the token-file path under ``artifacts_root``."""
    return artifacts_root / TOKEN_FILENAME


def ensure_daemon_token(artifacts_root: Path) -> str:
    """Return the daemon's auth token, generating + persisting one if needed.

    Resolution order:
      1. ``LIVE_RUNNER_DAEMON_TOKEN`` env on the daemon's process.
      2. Contents of ``artifacts_root/.host-daemon-token`` on disk.
      3. A freshly-generated token written to (2), then returned.

    Always returns a non-empty token — auth is mandatory; there is no
    "open" mode (the loopback-only bind that previously stood in for auth is
    gone, see ADR 0007).
    """
    env_token = os.environ.get(TOKEN_ENV_VAR)
    if env_token:
        return env_token

    path = token_file_path(artifacts_root)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RuntimeError(f"cannot read daemon token file {path}: {exc}") from exc

    return _write_new_token(path)


def read_daemon_token(artifacts_root: Path) -> str | None:
    """Read the token from env or the on-disk file, without generating one.

    Used by the data plane's ``host_daemon_client``: env preferred, then the
    file-backed fallback the daemon writes. Returns ``None`` when neither source
    has a token — the caller then sends no header and the daemon returns 401.
    """
    env_token = os.environ.get(TOKEN_ENV_VAR)
    if env_token:
        return env_token

    path = token_file_path(artifacts_root)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        return existing or None
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("cannot read daemon token file %s: %s", path, exc)
        return None


def _write_new_token(dest: Path) -> str:
    """Generate a fresh token and write it to ``dest`` atomically.

    Atomic via ``tempfile.mkstemp`` + ``os.replace``: a crash mid-write must not
    leave a half-formed file, because both sides treat an empty file as "no
    token" and the daemon would then 401 the data plane on every request.
    """
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(prefix=".host-daemon-token-", dir=str(dest.parent))
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
        # ``os.replace`` is atomic on the same filesystem; the token is
        # materialized iff this call returns.
        os.replace(tmp_path, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise
    try:
        os.chmod(dest, _TOKEN_FILE_MODE)
    except OSError as exc:
        logger.info("could not chmod daemon token %s: %s", dest, exc)
    logger.info("generated new host-daemon token at %s", dest)
    return token
