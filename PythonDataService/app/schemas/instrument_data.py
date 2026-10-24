"""Wire contract for one instrument's data in the lake (``GET /api/data-lake/instrument-data``).

The Data tab of an instrument's place reads this: for a window, which of the
calendar's sessions a run can read, which it cannot and why, and which of
those a Fill can repair. Every temporal value is ``int64 ms UTC`` (ADR 0022);
a session is carried as its scheduled open, a Fill window as the calendar
anchor ``DataRunSpec`` takes (12:00 UTC of the date).
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

#: Where one scheduled session stands against what a run needs.
#:
#: ``ready`` a run can read it. ``building`` a writer holds its claim now.
#: ``needs_rebuild`` the catalog calls it complete and a run still refuses it
#: (``reason`` names the evidence). ``missing`` the catalog has no row.
#: ``failed`` the last fetch failed (``reason`` is the catalog's own). ``forming``
#: the session has started and its tape is not finished, so nothing immutable
#: can exist for it. ``upcoming`` it has not started.
SessionState = Literal["ready", "building", "needs_rebuild", "missing", "failed", "forming", "upcoming"]

#: Why a session that is not ready is beyond a Fill.
#: ``before_provider_history`` it is older than the provider serves, so it can
#: never be fetched again (a retained copy stays readable). ``provider_no_data``
#: the provider answered the last fetch with no bars for it.
Blocker = Literal["before_provider_history", "provider_no_data"]

#: The derived daily file a daily-resolution run reads, which a minute run never opens.
#: ``current`` it matches the sessions the lake holds now. ``stale`` it was built from
#: another set of sessions, or its adjustment evidence no longer holds. ``not_built`` none exists.
RollupState = Literal["current", "stale", "not_built"]
RollupStaleReason = Literal["sources_changed", "adjustment_evidence_invalid"]


class InstrumentSession(BaseModel):
    trading_date_ms: int
    state: SessionState
    artifact_id: int | None = None
    #: ``needs_rebuild``: the evidence a run refuses (``adjustment_record_missing``...).
    #: ``failed``: the reason the catalog recorded. Absent for every other state.
    reason: str | None = None
    #: Whether a Fill asks the provider for this session. Meaningful only for
    #: ``needs_rebuild``, ``missing`` and ``failed``.
    fetchable: bool = False
    blocked_by: Blocker | None = None


class InstrumentDataSummary(BaseModel):
    #: Calendar days in the window, both ends included.
    calendar_day_count: int
    #: Of those, the days the canonical calendar schedules: weekends and holidays are not sessions.
    session_count: int
    ready: int
    building: int
    needs_rebuild: int
    missing: int
    failed: int
    forming: int
    upcoming: int
    #: ``needs_rebuild`` + ``missing`` + ``failed`` sessions a Fill can fetch.
    fetchable: int
    #: Sessions that are not ready and older than the provider serves.
    before_provider_history: int
    #: Sessions that are not ready because the provider returned no bars.
    provider_no_data: int
    #: Every finished session in the window is ready. False for a window with none.
    run_readable: bool


class FillWindow(BaseModel):
    """The span a Fill asks the admission for: first through last fetchable session."""

    start_trading_date_ms: int
    end_trading_date_ms: int


class DailyRollup(BaseModel):
    """The symbol's derived daily file in this tree. It is symbol-wide, not scoped to the window."""

    state: RollupState
    reason: RollupStaleReason | None = None


class InstrumentDataResponse(BaseModel):
    market: str
    symbol: str
    data_type: Literal["trade"] = "trade"
    resolution: Literal["minute"] = "minute"
    price_adjustment_mode: str
    data_root_id: UUID
    #: The oldest day the provider still serves, as the calendar anchor ``/backfill-defaults`` uses.
    provider_history_start_ms: int
    #: When this was read. A session's state is the lake's at this instant.
    as_of_ms: int
    summary: InstrumentDataSummary
    sessions: list[InstrumentSession]
    fill_window: FillWindow | None = None
    daily_rollup: DailyRollup
