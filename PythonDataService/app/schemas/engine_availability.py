"""Data-availability wire contract for the LEAN on-disk readers (#2499).

One transport schema for ``GET /api/engine/data/availability``, converted
from the engine's :class:`~app.engine.data.availability.AvailabilityReport`
by :meth:`AvailabilityResponse.from_report` — the single place the report's
``date``-typed facts meet the wire, where trading days anchor to their
scheduled session open as ``int64 ms UTC`` (temporal rigor: wire instants
are epoch milliseconds, never ISO date strings).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from app.engine.data.availability import AvailabilityReport


class UnreadableFileResponse(BaseModel):
    """A data file that is on disk but its reader cannot decode (#2489, #2499)."""

    path: str
    reason: str


class AvailabilityResponse(BaseModel):
    symbol: str
    start: str
    end: str
    resolution: str
    expected_days: int
    available_days: int
    is_complete: bool
    missing_days: list[str] = []
    # Sessions whose file is present but undecodable — the window is refused
    # and a backfill does not repair it, so the caller must see both the
    # sessions and the files at fault, not just ``is_complete=false``. Each
    # day anchors at its scheduled session open, int64 ms UTC. Required: the
    # endpoint always answers both.
    unreadable_days: list[int]
    unreadable_files: list[UnreadableFileResponse]
    # Per-root breakdown (reference mount vs cache) so the UI can tell
    # the user where the data is coming from.
    sources: dict[str, list[str]] = Field(default_factory=dict)

    @classmethod
    def from_report(cls, report: AvailabilityReport) -> AvailabilityResponse:
        """The one conversion from the engine report to the wire shape."""
        from app.lean_sidecar.trading_calendar import session_open_ms_utc

        return cls(
            symbol=report.symbol,
            start=report.start.isoformat(),
            end=report.end.isoformat(),
            resolution=report.resolution,
            expected_days=report.expected_days,
            available_days=report.available_days,
            is_complete=report.is_complete,
            missing_days=[d.isoformat() for d in report.missing_days],
            unreadable_days=[session_open_ms_utc(d) for d in report.unreadable_days],
            unreadable_files=[
                UnreadableFileResponse(path=file.path, reason=file.reason)
                for file in report.unreadable_files
            ],
            sources={
                root: [d.isoformat() for d in dates] for root, dates in report.sources.items()
            },
        )
