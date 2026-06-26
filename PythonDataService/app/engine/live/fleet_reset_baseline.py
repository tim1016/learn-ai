"""Operator-approved account reset baseline for fleet redeploys.

The cold-start reconciliation classifier is intentionally strict: broker
executions from an unknown namespace normally poison startup. A deliberate
account-wide reset is the exception, but only when an explicit artifact says
which flat account and which new strategy instances may ignore prior broker
history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FleetResetBaseline:
    account_id: str
    baseline_at_ms: int
    applies_to_strategy_instance_ids: frozenset[str]


def baseline_path(live_runs_root: Path, account_id: str) -> Path:
    """Return the single current baseline path for ``account_id``."""
    return live_runs_root / "fleet_baselines" / f"{account_id}.json"


def read_applicable_baseline(
    *,
    live_runs_root: Path,
    account_id: str,
    strategy_instance_id: str,
) -> FleetResetBaseline | None:
    """Read a flat-account baseline if it explicitly applies to this bot.

    Invalid or non-applicable files are treated as absent. Startup remains
    fail-closed because the classifier only receives an ignore cutoff when this
    function returns a concrete baseline.
    """
    path = baseline_path(live_runs_root, account_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("account_id", "")).upper() != account_id.upper():
        return None
    try:
        baseline_at_ms = int(payload["baseline_at_ms"])
    except (KeyError, TypeError, ValueError):
        return None
    if baseline_at_ms < 0:
        return None
    if payload.get("positions") != [] or payload.get("open_orders") != []:
        return None
    raw_ids = payload.get("applies_to_strategy_instance_ids")
    if not isinstance(raw_ids, list):
        return None
    applies = frozenset(str(v) for v in raw_ids if isinstance(v, str))
    if strategy_instance_id not in applies:
        return None
    return FleetResetBaseline(
        account_id=account_id,
        baseline_at_ms=baseline_at_ms,
        applies_to_strategy_instance_ids=applies,
    )


__all__ = ["FleetResetBaseline", "baseline_path", "read_applicable_baseline"]
