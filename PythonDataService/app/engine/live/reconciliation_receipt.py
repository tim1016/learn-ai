"""Durable writer for ``reconciliation_receipt.json`` (ADR-0008 §5).

Atomic-replace writer using the same crash-safety contract as
``LiveStateSidecarRepo``: serialize to a sibling ``.tmp``, ``fh.flush()`` +
``os.fsync()``, ``os.replace()``, then ``_fsync_parent_dir`` under a
``_file_lock``. The cold-start orchestrator (this module's sole caller)
writes an ``in_progress`` sentinel before doing any broker work and then
replaces it with the verdict ``passed`` / ``failed`` receipt — so a crash
mid-reconcile leaves an honest "we never finished" marker rather than the
previous run's stale ``passed`` evidence.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from app.engine.live.live_state_sidecar import _file_lock, _fsync_parent_dir
from app.schemas.live_runs import ReconciliationReceipt

RECEIPT_FILENAME = "reconciliation_receipt.json"


def write_receipt(run_dir: Path, receipt: ReconciliationReceipt) -> Path:
    """Atomically persist ``receipt`` under ``run_dir/reconciliation_receipt.json``.

    Same crash-safety contract as the live-state sidecar: tempfile write +
    fsync + ``os.replace`` + parent-dir fsync, all under an advisory file
    lock so concurrent writers can't race on the shared tempfile name.

    Returns the path written.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / RECEIPT_FILENAME
    with _file_lock(path):
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        payload = receipt.model_dump_json().encode("utf-8")
        with open(tmp_path, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.replace(tmp_path, path)
        except Exception:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
            raise
        _fsync_parent_dir(path)
    return path


__all__ = ["RECEIPT_FILENAME", "write_receipt"]
