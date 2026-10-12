"""Run records, headers and stored sessions built by hand, for the tests of a saved run's closer look that need no database (#3030).

A header is what ``saved_runs.get_run_header`` makes of a row: the run's record and its setup, or the reason the setup
cannot be read. Building one here, from the same dataclasses, tests the decisions made on a header (is the run complete,
may these two runs be compared) with no store behind them.
"""

from __future__ import annotations

from typing import Any

from app.options_backtest.run import RunResult
from app.options_backtest.run_detail import SessionRecord
from app.options_backtest.saved_runs import PresentedStatus, RunHeader, RunRecord, StoredSession
from tests.options_backtest.setup_fixtures import setup

CREATED_AT_MS = 1_791_800_000_000
THE_REASON = "lots: Extra inputs are not permitted"


def a_record(
    run_id: int = 1,
    first: int = 10,
    last: int = 30,
    requested: int = 3,
    *,
    status: PresentedStatus = "complete",
    underlying: str = "SPY",
    created_at_ms: int = CREATED_AT_MS,
    setup_name: str | None = None,
    fee_policy_text: str = "Fees are not charged, so results are before fees.",
) -> RunRecord:
    return RunRecord(
        id=run_id,
        created_at_ms=created_at_ms,
        finished_at_ms=created_at_ms + 100_000,
        job_id=f"job-{run_id}",
        status=status,
        setup_name=setup_name,
        underlying=underlying,
        first_session_open_ms=first,
        last_session_open_ms=last,
        sessions_requested=requested,
        fee_policy_text=fee_policy_text,
        calendar_identity="NYSE",
        calendar_package_version="5.1.0",
        failure=None,
        headline=None,
    )


def a_header(run_id: int = 1, first: int = 10, last: int = 30, requested: int = 3, **record: Any) -> RunHeader:
    """A readable header: a setup that validates, which ``setup_changes`` of the base setup alter."""
    changes = record.pop("setup_changes", {})
    return RunHeader(record=a_record(run_id, first, last, requested, **record), setup=setup(**changes))


def an_unreadable_header(run_id: int = 1, reason: str = THE_REASON, **record: Any) -> RunHeader:
    """The header of a run whose stored setup no longer validates: no setup, and the reason."""
    return RunHeader(record=a_record(run_id, **record), setup=None, setup_cannot_open_reason=reason)


def a_session_record(result: RunResult, at: int, *, readable: bool = True, run_id: int = 7) -> SessionRecord:
    """Session number ``at`` of a replayed run as the store reads it back, with its stored profit.

    ``readable=False`` is the same run with its setup lost (the reason ``THE_REASON``).
    """
    session = result.sessions[at]
    record = a_record(
        run_id,
        result.sessions[0].session_open_ms,
        result.sessions[-1].session_open_ms,
        len(result.sessions),
        underlying=result.setup.underlying,
    )
    header = (
        RunHeader(record=record, setup=result.setup)
        if readable
        else RunHeader(record=record, setup=None, setup_cannot_open_reason=THE_REASON)
    )
    return SessionRecord(header=header, session=StoredSession(result=session, profit_usd=session.pnl_usd))
