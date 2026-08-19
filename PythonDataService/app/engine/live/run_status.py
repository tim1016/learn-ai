"""Shared atomic JSON writer retained for active Alpaca bot artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write payload to path atomically via a temp file + rename."""
    tmp = path.with_suffix(".tmp")
    data = json.dumps(payload, default=str)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)
