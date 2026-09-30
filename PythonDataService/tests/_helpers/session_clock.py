"""Pin the process wall clock inside a regular session (#2596).

The Clerk refuses a market ENTER that could not reach the broker inside the
canonical calendar's regular session, judged at its repository clock. A suite
whose Clerk reads the host's wall clock would pass by day and fail by night, so
it pins that clock instead: one controllable source backs every imported and
default ``now_ms_utc`` callable, starting a minute after a regular open and
advancing in real time from there.
"""

from __future__ import annotations

from datetime import date
from time import monotonic
from types import SimpleNamespace

import pytest

from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.utils import timestamps

IN_SESSION_DAY = date(2026, 9, 25)
"""A full trading day with every regulatory fee rate pinned."""


def pin_wall_clock_in_session(monkeypatch: pytest.MonkeyPatch) -> int:
    """Start the wall clock at 09:31 ET on :data:`IN_SESSION_DAY`; return that instant."""
    return pin_wall_clock_at(monkeypatch, session_open_ms_utc(IN_SESSION_DAY) + 60_000)


def pin_wall_clock_at(monkeypatch: pytest.MonkeyPatch, start_ms: int) -> int:
    """Start the wall clock at ``start_ms``; a later call moves it, as a restart days later would."""
    started = monotonic()
    monkeypatch.setattr(
        timestamps, "time", SimpleNamespace(time=lambda: start_ms / 1000 + monotonic() - started)
    )
    return start_ms
