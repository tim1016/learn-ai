"""``RunLedger`` — immutable run-identity record.

A ledger entry captures every input that, taken together, determines
the result of a backtest run. Two runs whose ledgers agree on
``strategy_spec_hash``, ``data_snapshot_id``, ``engine_version``,
fill/cost fields, and ``random_seed`` are guaranteed (by the
deterministic engine) to produce identical results — so their
``result_hash``, ``trade_log_hash``, and ``metrics_hash`` must match.
The EMA acceptance gate enforces this invariant.

Forward-compat fields included in v1 because they're cheap now and ugly
to migrate later:
  * ``parent_run_id`` — set on walk-forward fold runs (Phase C),
    Monte Carlo simulation runs (Phase D), sensitivity-sweep child runs
    (Phase E)
  * ``parent_spec_hash`` — set on sensitivity grid runs (Phase E) so the
    grid can be rolled up by spec lineage
  * ``random_seed`` — recorded even though the engine is currently
    deterministic without an RNG; locks Phase D's reshuffle/resample
    determinism contract before MC code lands
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ENGINE_VERSION = "0.1.0"
"""Bumped on engine semantic change. See ``docs/references/run-ledger.md``."""

logger = logging.getLogger(__name__)

_GIT_COMMIT_CACHE: str | None = None


def _capture_git_commit() -> str:
    """Return the repo's ``HEAD`` SHA, captured once per process.

    Captured at first call and memoized — the value is constant for the
    lifetime of the process. Falls back to ``"unknown"`` when git is
    unavailable, the working tree isn't a git checkout, or the call
    times out. The ledger field exists so future replays can detect
    "ran on a different commit"; it's not load-bearing for correctness.
    """
    global _GIT_COMMIT_CACHE
    if _GIT_COMMIT_CACHE is not None:
        return _GIT_COMMIT_CACHE
    try:
        # Run from this file's directory — guarantees we land in the right
        # repo even when CWD differs (e.g., container running from /).
        cwd = Path(__file__).resolve().parent
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            cwd=str(cwd),
            check=False,
        )
        if proc.returncode == 0:
            sha = proc.stdout.strip()
            if sha:
                _GIT_COMMIT_CACHE = sha
                return sha
        logger.debug(
            "[RUNS] git rev-parse HEAD returned %d; engine_git_commit will be 'unknown'",
            proc.returncode,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        # FileNotFoundError → git binary missing (CI image without git).
        # TimeoutExpired → process hang. OSError → fork/exec failure.
        # Any of these are diagnostic, not fatal: the ledger field is
        # informational, not part of the deterministic identity contract.
        logger.debug("[RUNS] git rev-parse HEAD unavailable: %s", exc)
    _GIT_COMMIT_CACHE = "unknown"
    return _GIT_COMMIT_CACHE


def now_ms_utc() -> int:
    """Return current wall-clock time as ``int64 ms`` since Unix epoch UTC."""
    return int(time.time() * 1000)


def resolve_data_root_revision() -> str:
    """Identify the LEAN data root for ``data_snapshot_id``.

    Strategy (best to fallback):
      1. Env var ``LEAN_DATA_ROOT_REVISION`` if explicitly set — lets
         CI/ops pin a revision string.
      2. ``git rev-parse HEAD`` of the data-root directory if it's a git
         working tree. Useful when the data is vendored or generated
         from a versioned source.
      3. ``mtime`` of the data-root directory in integer seconds — a
         coarse "did this change?" proxy.
      4. ``"unknown"``.

    The revision string is part of ``data_snapshot_id``, so changing it
    invalidates ledger identity. Callers that need stricter content
    addressability (e.g. CI parity tests across machines) should set
    ``LEAN_DATA_ROOT_REVISION`` explicitly.
    """
    explicit = os.environ.get("LEAN_DATA_ROOT_REVISION")
    if explicit:
        return explicit

    for env_var in ("LEAN_DATA_ROOT", "LEAN_DATA_CACHE"):
        val = os.environ.get(env_var)
        if not val:
            continue
        root = Path(val)
        if not root.is_dir():
            continue
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2.0,
                cwd=str(root),
                check=False,
            )
            if proc.returncode == 0:
                sha = proc.stdout.strip()
                if sha:
                    return sha
            logger.debug(
                "[RUNS] data root %s: git rev-parse returned %d; "
                "falling back to mtime",
                root,
                proc.returncode,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.debug(
                "[RUNS] data root %s: git unavailable (%s); falling back to mtime",
                root,
                exc,
            )
        try:
            return f"mtime:{int(root.stat().st_mtime)}"
        except OSError as exc:
            logger.debug(
                "[RUNS] data root %s: mtime unavailable (%s); falling back to 'unknown'",
                root,
                exc,
            )
    return "unknown"


class RunLedger(BaseModel):
    """Immutable identity record for a single strategy run.

    Persisted as ``ledger.json`` in the run's artifact directory.
    Pydantic ``extra='forbid'`` makes schema drift loud rather than
    silent — adding a field is a deliberate ``schema_version`` bump.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    run_id: str

    # Lineage — set on child runs (folds, MC simulations, sweep points).
    parent_run_id: str | None = None
    parent_spec_hash: str | None = None

    # Spec identity.
    strategy_spec_id: str  # caller-provided label or fixture name
    strategy_spec_hash: str  # sha256(canonical_json(spec))
    strategy_spec_json: dict  # verbatim spec snapshot

    # Engine identity.
    engine_name: Literal["learn_ai_event_driven"] = "learn_ai_event_driven"
    engine_version: str = ENGINE_VERSION
    engine_git_commit: str

    # Run config.
    symbol: str
    resolution_minutes: int = Field(ge=1)
    start_ms: int
    end_ms: int
    initial_cash: float = Field(ge=0.0)
    fill_mode: str  # "signal_bar_close" | "next_bar_open"
    commission_per_order: float = Field(ge=0.0)
    slippage_per_share: float = Field(default=0.0, ge=0.0)
    warmup_policy: Literal["spec_indicator_warmup"] = "spec_indicator_warmup"
    random_seed: int = 0

    # Data identity.
    data_source: Literal["lean_minute_reader"] = "lean_minute_reader"
    data_snapshot_id: str

    # Result identity (filled after the run completes).
    result_hash: str | None = None
    trade_log_hash: str | None = None
    metrics_hash: str | None = None

    # Lifecycle.
    created_at_ms: int = Field(default_factory=now_ms_utc)
    completed_at_ms: int | None = None
    status: Literal["running", "completed", "failed"] = "running"
    failure_reason: str | None = None
