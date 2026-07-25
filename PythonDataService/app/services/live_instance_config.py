"""Read-only live-instance configuration access used by catalog projections."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from app.routers.live_runs import _read_ledger


def live_config_for_run_dir(run_dir: Path) -> Mapping[str, object] | None:
    """Return the durable configuration when its ledger is readable."""

    try:
        ledger = _read_ledger(run_dir)
    except (OSError, json.JSONDecodeError):
        return None
    live_config = ledger.get("live_config")
    return live_config if isinstance(live_config, Mapping) else None
