"""Rows the Recency Chart repository returns (PRD #1927)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MembershipView:
    recency_run_id: int
    study_id: int | None
    created_at_ms: int


@dataclass(frozen=True)
class TradeView:
    """One trade as the chart reads it: the representative run's identity plus every live membership."""

    symbol: str
    strategy_key: str
    params_hash: str
    params_json: str
    fingerprint: str
    entry_ms: int
    exit_ms: int
    pnl_pts: float
    pnl_pct: float
    quantity: float
    pnl: float
    holding_sessions: int
    sharpe: float | None
    study_id: int | None
    recency_run_id: int
    is_synthetic_exit: bool
    signal_reason: str
    memberships: list[MembershipView]


@dataclass(frozen=True)
class PersistOutcome:
    recency_run_id: int | None
    # The launch was tombstoned: nothing was written, and that is a successful no-op.
    skipped: bool = False
    # The launch already held this cell: the existing run is returned, nothing is written or counted.
    redelivered: bool = False


@dataclass(frozen=True)
class LaunchView:
    """One launch as the resume gate and the launches list read it (#1938).

    ``status`` is the stored vocabulary (``RUNNING`` / ``COMPLETED`` /
    ``FAILED`` / ``CANCELLED``); presentation (``interrupted`` et al.) is the
    router's job, asked from the job liveness only when it can change the
    answer.
    """

    launch_id: str
    status: str
    job_id: str | None
    attempt: int
    expected_runs: int
    succeeded_runs: int
    failed_runs: int
    created_at_ms: int
    completed_at_ms: int | None
    deleted_at_ms: int | None
    config_json: str
