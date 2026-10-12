"""One stored session of a saved run, put together for the one-day timeline (#3030).

The route reads the session's rows and hands them here; what a timeline draws is worked out in this module and the
three it calls, so none of it needs HTTP or a router to be tested:

* ``session_levels``: the lines, the fills and the marks, from the stored trades and events;
* ``session_series``: the price series, read from the lake and shown only if the file is the one the run read.

The stored facts (status, cash, profit, trades, events, and the lines, fills and marks worked from them) never depend on
the lake: whatever happens to a series, they are served. A run whose stored setup no longer validates is served the
same, with the lines that do not need the setup (see ``session_levels``) and its underlying from the stored record.

``SessionRecordInconsistent`` (a stored session whose trades and events disagree, which the replay cannot make) is not
caught here: the caller says what it means.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.options_backtest.data import RunData
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.run_detail import SessionRecord
from app.options_backtest.session_levels import ChartedFill, ChartedMark, Level, fills_of, levels_of, marks_of
from app.options_backtest.session_series import SessionSeries, read_session_series


@dataclass(frozen=True)
class SessionView:
    """A stored session with everything a timeline draws for it."""

    record: SessionRecord
    series: SessionSeries
    levels: tuple[Level, ...]
    fills: tuple[ChartedFill, ...]
    marks: tuple[ChartedMark, ...]


def build_session_view(record: SessionRecord, data: RunData) -> SessionView:
    """The session's lines, fills, marks and series. Blocking: ``data`` reads files, so call it from a worker thread.

    ``SessionRecordInconsistent`` when the stored trades and events do not agree; the lake is not asked then.
    """
    outcome = record.session.result.outcome
    levels = levels_of(record.header.setup, outcome)
    # The underlying is stored on the run's own record, so it is known even when the setup can no longer be read.
    series = read_session_series(record.session.result, underlying=record.header.record.underlying, data=data)
    return SessionView(
        record=record, series=series, levels=levels, fills=fills_of(outcome.trades), marks=marks_of(outcome.trades)
    )


async def session_view_of(record: SessionRecord, data: RunData | None = None) -> SessionView:
    """``build_session_view`` in a worker thread, the lake's readers blocking on files and on the catalog.

    ``data`` is the run's underlying read from the data lake unless a caller hands another.
    """
    reader = LakeRunData(record.header.record.underlying) if data is None else data
    return await asyncio.to_thread(build_session_view, record, reader)
