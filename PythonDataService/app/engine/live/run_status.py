"""Atomic run_status.json sidecar writer for cmd_start lifecycle events.

The sidecar gives the live-runs router an authoritative record of
started_at_ms, ended_at_ms, exit_code, and exit_reason without grepping
live.log. Writes are atomic (tmp → fsync → rename) to prevent the router
reading a partial file.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.schemas.live_runs import RunStatusSidecar


def now_ms() -> int:
    """Current time as int64 ms UTC."""
    return int(time.time() * 1000)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write payload to path atomically via a temp file + rename."""
    tmp = path.with_suffix(".tmp")
    data = json.dumps(payload, default=str)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def write_run_status(run_dir: Path, sidecar: RunStatusSidecar) -> None:
    """Persist the sidecar to <run_dir>/run_status.json atomically."""
    path = run_dir / "run_status.json"
    _atomic_write_json(path, sidecar.model_dump())
