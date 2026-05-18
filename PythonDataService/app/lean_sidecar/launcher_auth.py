"""Shared-secret auth between the data plane and the launcher.

Open-Q1 review-fix. The launcher exposes ``/launch`` (spawns LEAN
images) and ``/extract-metadata`` (spawns ``podman create + cp``);
anyone who can reach the launcher's port can use the host's container
runtime. The shared-secret token is the only auth between the
data-plane container and that capability.

Previously the token was opt-in: ``LEAN_LAUNCHER_TOKEN`` env unset on
either side → the launcher's auth check is bypassed entirely
(``_expected_token()`` returns None, so the header check is a no-op).
That was acceptable for a Phase 1 spike running on a single host with
the launcher bound to 127.0.0.1 only. With Phase 4c accepting
arbitrary user source and the launcher now bound to ``0.0.0.0`` so
the data-plane container can reach it through the WSL2 adapter IP,
"opt-in token" means "no auth in practice".

This module enforces mandatory auth: the launcher generates a token
at startup if env is unset and writes it to a shared bind-mounted
file the data plane reads. Operators who want to set their own token
continue to via the env var — the file-backed fallback only activates
when both sides have no env override.

Wire format: ``X-Launcher-Token: <token>`` header on every request.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import tempfile
from pathlib import Path

from app.lean_sidecar.config import DEFAULT_ARTIFACTS_ROOT

logger = logging.getLogger(__name__)


# File the launcher writes the auto-generated token to + the data
# plane reads it from. Lives at the artifacts-root level (sibling to
# the per-run subdirectories) so both processes view the same bytes
# through the shared bind mount. Hidden ``.launcher-token`` prefix so
# a ``ls`` of artifacts doesn't surface it as a run.
LAUNCHER_TOKEN_FILENAME = ".launcher-token"

# Tokens are 32 bytes URL-safe base64 — ~256 bits of entropy, more
# than enough for a host-local shared secret. Anything shorter risks
# being brute-forceable on a fast LAN; anything longer is needless.
_TOKEN_BYTES = 32

# Restrictive POSIX mode on the token file. The data-plane container
# user reads it through the bind mount; non-launcher users on the
# host (other developers sharing the workstation) must not.
_TOKEN_FILE_MODE = 0o600


def token_file_path(artifacts_root: Path | None = None) -> Path:
    """Resolve the canonical token-file path.

    Defaults to ``DEFAULT_ARTIFACTS_ROOT / LAUNCHER_TOKEN_FILENAME``;
    callers (the launcher) may override when they resolve a different
    artifacts root via ``LEAN_LAUNCHER_ARTIFACTS_ROOT``.
    """
    root = artifacts_root if artifacts_root is not None else DEFAULT_ARTIFACTS_ROOT
    return root / LAUNCHER_TOKEN_FILENAME


def ensure_launcher_token(artifacts_root: Path) -> str:
    """Return the launcher's auth token, generating + persisting one
    if neither env nor the on-disk file already carries it.

    Called by the launcher at request time (NOT just at startup) so a
    stale env-cleared restart picks up the persisted file without
    needing a manual rewrite. Idempotent: repeated calls return the
    same token until something rotates the env or deletes the file.

    Resolution order:
      1. ``LEAN_LAUNCHER_TOKEN`` env on the launcher's process.
      2. Contents of ``artifacts_root/.launcher-token`` on disk.
      3. Freshly-generated 32-byte URL-safe token written to (2),
         then returned.

    Operators who want a stable known token set the env var; everyone
    else gets a token that's stable across launcher restarts as long
    as the artifacts root persists.
    """
    env_token = os.environ.get("LEAN_LAUNCHER_TOKEN")
    if env_token:
        return env_token

    path = token_file_path(artifacts_root)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError as e:
        # Don't fall through to generating a new one if the file
        # exists but we can't read it — that'd hide a permissions
        # problem operators would want to see.
        raise RuntimeError(
            f"cannot read launcher token file {path}: {e}"
        ) from e

    return _write_new_token(path)


def read_launcher_token(artifacts_root: Path | None = None) -> str | None:
    """Read the launcher token from env or the on-disk file.

    Used by the data plane's ``launcher_client``: env preferred, then
    the file-backed fallback the launcher writes. Returns ``None``
    when neither source has a token — caller then sends no header,
    and the launcher returns 401.
    """
    env_token = os.environ.get("LEAN_LAUNCHER_TOKEN")
    if env_token:
        return env_token

    path = token_file_path(artifacts_root)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        return existing or None
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("cannot read launcher token file %s: %s", path, e)
        return None


def _write_new_token(dest: Path) -> str:
    """Generate a fresh token and write it to ``dest`` atomically.

    Atomic via ``tempfile.NamedTemporaryFile`` + ``os.replace``: a
    crash mid-write must not leave the file half-formed, because both
    sides will silently treat an empty file as "no token" and the
    launcher will then 401 the data plane on every request.
    """
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=".launcher-token-",
        dir=str(dest.parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
        # ``os.replace`` is atomic on the same filesystem. The token
        # is materialized iff this call returns.
        os.replace(tmp_path, dest)
    except BaseException:
        # Best-effort cleanup on any failure path — exceptions
        # propagate; we just don't leave a tempfile turd.
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise
    try:
        os.chmod(dest, _TOKEN_FILE_MODE)
    except OSError as e:
        # Mode-tightening can fail on Windows + bind mounts where the
        # NTFS ACL doesn't map cleanly. Log and continue — the token
        # is still secret-enough to fulfill its job; this is
        # defense-in-depth.
        logger.info("could not chmod launcher token %s: %s", dest, e)
    logger.info("generated new launcher token at %s", dest)
    return token
