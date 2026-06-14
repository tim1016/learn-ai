"""Per-session forensic sidecar for the live engine — VCR-0006 / Phase 3.

Captures the verified identity pair (``ledger_account_id``,
``connected_account``) at session start and the ``connection_epoch`` that
increments on each successful (re)connect. Persisted to
``artifacts/live_runs/<run_id>/session_metadata.json`` so a later audit can
reconstruct who the run was actually placing orders for.

Per-row ``connected_account`` on every executions parquet entry was rejected
in PRD §11 (C) — the start-time / reconnect check halts before further orders
are trusted, so per-session is sufficient and less noisy.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SESSION_METADATA_FILENAME = "session_metadata.json"
SESSION_METADATA_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SessionMetadata:
    """The verified identity pair plus the (re)connect counter.

    ``connection_epoch`` starts at 1 on the first successful connect and
    increments on each subsequent (re)connect — the forensic record makes
    a swap-on-reconnect (Phase 3.1 follow-up) reconstructable even if the
    halt event arrives later than the broker's own logs.
    """

    schema_version: int
    ledger_account_id: str
    connected_account: str
    session_started_ms: int
    session_ended_ms: int | None
    connection_epoch: int

    def to_json_dict(self) -> dict:
        d = asdict(self)
        # ``None`` for ``session_ended_ms`` round-trips through JSON cleanly
        # as ``null`` — no special handling.
        return d


def write_session_metadata(run_dir: Path, metadata: SessionMetadata) -> Path:
    """Persist the session sidecar atomically.

    Uses ``write-temp + rename`` (atomic on POSIX) so a crash mid-write
    never leaves a half-written JSON file the cockpit would refuse to
    parse. Overwrites any prior content — the latest session wins for a
    given ``run_dir``.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / SESSION_METADATA_FILENAME
    payload = json.dumps(metadata.to_json_dict(), indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".session_metadata.", suffix=".tmp", dir=str(run_dir)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup of the tempfile if rename failed
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def read_session_metadata(run_dir: Path) -> SessionMetadata | None:
    """Read the prior session sidecar, if any. Returns ``None`` if absent
    or malformed (the caller writes a fresh one)."""
    path = run_dir / SESSION_METADATA_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("session_metadata.json at %s is unreadable; treating as missing", path)
        return None
    try:
        return SessionMetadata(
            schema_version=int(payload.get("schema_version", SESSION_METADATA_SCHEMA_VERSION)),
            ledger_account_id=str(payload["ledger_account_id"]),
            connected_account=str(payload["connected_account"]),
            session_started_ms=int(payload["session_started_ms"]),
            session_ended_ms=(
                int(payload["session_ended_ms"])
                if payload.get("session_ended_ms") is not None
                else None
            ),
            connection_epoch=int(payload.get("connection_epoch", 1)),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("session_metadata.json at %s has wrong shape; treating as missing", path)
        return None
