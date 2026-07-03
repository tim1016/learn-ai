"""Run-directory lookup helpers shared by CLI and router surfaces."""

from __future__ import annotations

import json
from pathlib import Path


def latest_run_dir_for_instance(artifacts_root: Path, strategy_instance_id: str) -> Path | None:
    """Find the newest ``live_runs/<run_id>/`` ledger for one strategy instance."""

    live_runs = artifacts_root / "live_runs"
    if not live_runs.exists():
        return None
    candidates: list[tuple[float, Path]] = []
    for run_dir in live_runs.iterdir():
        if not run_dir.is_dir():
            continue
        ledger_path = run_dir / "run_ledger.json"
        if not ledger_path.exists():
            continue
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if ledger.get("strategy_instance_id") != strategy_instance_id:
            continue
        try:
            mtime = ledger_path.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, run_dir))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def account_id_from_run_ledger(run_dir: Path) -> str | None:
    """Return the account id from a run ledger when present and readable."""

    ledger_path = run_dir / "run_ledger.json"
    if not ledger_path.exists():
        return None
    try:
        payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    account_id = payload.get("account_id") if isinstance(payload, dict) else None
    if not isinstance(account_id, str) or not account_id:
        return None
    return account_id
