"""Atomic per-run ``OperatorIncident`` writer + unresolved-incident reader.

Each incident is stored as a separate JSON file under
``<run_dir>/operator_incidents/<incident_id>.json``.  The write protocol
mirrors ``reconciliation_receipt.py``: tmp write + fsync + ``os.replace`` +
parent-dir fsync, all under an advisory file lock so concurrent writers
serialise without racing on the tempfile name.

``list_unresolved()`` scans the directory for every incident whose
``resolved_at_ms`` is ``None``, letting the post-halt gate (PR 2 Task 5)
check for blocking conditions before the engine enters its bar loop.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir
from app.operator.notices.schema import OperatorIncident

logger = logging.getLogger(__name__)

INCIDENTS_DIR = "operator_incidents"


class IncidentStore:
    """Per-run store for ``OperatorIncident`` records.

    Args:
        run_dir: The run's artifact directory.  The ``operator_incidents/``
            sub-directory is created on first write.
    """

    def __init__(self, run_dir: Path) -> None:
        self._dir = run_dir / INCIDENTS_DIR

    # ------------------------------------------------------------------
    # Write / amend
    # ------------------------------------------------------------------

    def append(self, incident: OperatorIncident) -> Path:
        """Atomically persist ``incident`` as ``<incident_id>.json``.

        Returns the path written.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{incident.incident_id}.json"
        return _atomic_write(path, incident.model_dump_json())

    def append_unless_resolved(self, incident: OperatorIncident) -> OperatorIncident:
        """Persist ``incident`` unless a resolved copy already exists.

        Returns the incident that remains authoritative on disk.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{incident.incident_id}.json"
        with _file_lock(path):
            if path.exists():
                existing = OperatorIncident.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                if existing.resolved_at_ms is not None:
                    return existing
            _atomic_write_locked(path, incident.model_dump_json())
        return incident

    def resolve(self, incident_id: str, resolved_at_ms: int) -> None:
        """Read, patch ``resolved_at_ms``, and atomically re-write the incident.

        Raises ``FileNotFoundError`` if the incident has never been written.
        """
        path = self._dir / f"{incident_id}.json"
        with _file_lock(path):
            raw = path.read_text(encoding="utf-8")
            incident = OperatorIncident.model_validate_json(raw)
            updated = incident.model_copy(update={"resolved_at_ms": resolved_at_ms})
            _atomic_write_locked(path, updated.model_dump_json())

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_unresolved(self) -> list[OperatorIncident]:
        """Return every incident whose ``resolved_at_ms`` is ``None``.

        Returns an empty list when the directory does not exist.
        """
        if not self._dir.exists():
            return []
        result: list[OperatorIncident] = []
        for p in sorted(self._dir.glob("*.json")):
            try:
                incident = OperatorIncident.model_validate_json(
                    p.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                # A record we can't parse is dropped, but this feeds the
                # post-halt safety gate — a silently-skipped unresolved incident
                # could let a bot restart that should stay blocked. Surface it
                # (e.g. a pre-schema incident missing actionability/resolution)
                # so it's visible during incident response instead of vanishing.
                logger.warning(
                    "skipping unparseable operator incident",
                    extra={"path": str(p), "exception": repr(exc)},
                )
                continue
            if incident.resolved_at_ms is None:
                result.append(incident)
        return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, payload_json: str) -> Path:
    """Acquire lock then delegate to ``_atomic_write_locked``."""
    with _file_lock(path):
        return _atomic_write_locked(path, payload_json)


def _atomic_write_locked(path: Path, payload_json: str) -> Path:
    """Write ``payload_json`` atomically (caller must hold the lock)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = payload_json.encode("utf-8")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _fsync_parent_dir(path)
    return path


__all__ = ["INCIDENTS_DIR", "IncidentStore"]
