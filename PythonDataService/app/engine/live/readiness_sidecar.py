"""Atomic read/write of the engine-authored readiness vector sidecar (ADR 0005).

The live engine overwrites ``readiness.json`` in its run dir each tick; the
status endpoint reads the latest. Single writer (the engine), many readers
(status polls), so an atomic tmp + ``os.replace`` write is sufficient — no lock.
The caller is responsible for confining ``run_dir`` (the reader builds it from a
remote-sourced run id and must apply the path-injection barrier).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

READINESS_FILE = "readiness.json"


def write_readiness(run_dir: Path, vector: dict) -> None:
    """Overwrite the readiness sidecar atomically (tmp + os.replace)."""
    path = run_dir / READINESS_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(vector), encoding="utf-8")
    os.replace(tmp, path)


def read_readiness(run_dir: Path) -> dict | None:
    """Return the latest readiness vector, or None when absent/unreadable."""
    try:
        return json.loads((run_dir / READINESS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
