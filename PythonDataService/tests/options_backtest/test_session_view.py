"""One session of a saved run put together for a timeline, with no router and no database (#3030).

The run is the three-session one of ``_detail_scene.py`` made over a lake in a temporary folder, so the series are read from
real stored files; the session is handed over as the store reads it back (``_headers.a_session_record``). Figures are
those worked in the scene's docstring and ``test_session_series.py``: on Wednesday the underlying has 390 points, the three
contracts have 55 (call), 56 (put 495) and 57 (put 500), and the session has 3 trades, 20 events, 8 lines, 6 fills and no
mark. Every call is made from a running event loop, as a route makes it, which the lake's readers refuse unless the view
moves the reading to a worker thread.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import respx

from app.options_backtest.results import EventKind
from app.options_backtest.session_levels import SessionRecordInconsistent
from app.options_backtest.session_series import SeriesReason
from app.options_backtest.session_view import session_view_of
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._detail_scene import CALL_500, PUT_495, PUT_500, run_the_scene
from tests.options_backtest._headers import THE_REASON, a_session_record
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import raw_root
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake


@respx.mock
async def test_the_busy_day_is_put_together_from_the_stored_rows_and_the_lake_from_a_running_event_loop(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()
    record = a_session_record(result, 0)
    setup_lost = a_session_record(result, 0, readable=False)
    assert setup_lost.header.setup is None and setup_lost.header.setup_cannot_open_reason == THE_REASON, (
        "the record of a run whose setup was lost has no setup and says why"
    )

    view = await session_view_of(record)
    view_without_setup = await session_view_of(setup_lost)

    assert view.record is record, "the view carries the record it was made from"
    assert (len(view.record.session.result.outcome.trades), len(view.record.session.result.outcome.events)) == (
        3,
        20,
    ), "the busy day has 3 trades and 20 events"
    assert (len(view.levels), len(view.fills), len(view.marks)) == (8, 6, 0), "the busy day: 8 lines, 6 fills, no mark"
    assert (view.series.underlying.reason, len(view.series.underlying.points)) == (None, 390), (
        "the underlying is read from the lake with 390 points"
    )
    assert [(each.symbol, each.reason, len(each.points)) for each in view.series.contracts] == [
        (CALL_500, None, 55),
        (PUT_495, None, 56),
        (PUT_500, None, 57),
    ], "the three contracts are read from the lake with 55, 56 and 57 points"

    # The underlying comes from the run's stored record, so its series is read and shown without the setup. The lines are
    # Wednesday's eight less the lag leg's momentum (its level comes from the rule): seven. Fills and marks need no setup.
    assert (view_without_setup.series.underlying.reason, len(view_without_setup.series.underlying.points)) == (
        None,
        390,
    ), "a run whose setup was lost still shows the underlying's series"
    assert len(view_without_setup.levels) == 7 and (
        len(view_without_setup.fills),
        len(view_without_setup.marks),
    ) == (6, 0), "a run whose setup was lost is drawn with every line but an unfilled momentum"


@respx.mock
async def test_what_is_stored_is_drawn_whatever_the_lake_holds_and_each_series_says_it_is_gone(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()
    # The lake loses every file the run read.
    for each in raw_root(tmp_lake).rglob("*_trade.zip"):
        each.unlink()

    view = await session_view_of(a_session_record(result, 0))

    assert (len(view.levels), len(view.fills), len(view.marks)) == (8, 6, 0)
    assert view.series.underlying.reason is SeriesReason.GONE
    assert {each.reason for each in view.series.contracts} == {SeriesReason.GONE}


@respx.mock
async def test_a_session_whose_stored_trades_and_events_disagree_is_not_drawn(
    fake_catalog: FakeCatalog, tmp_lake: Path
) -> None:
    result = await run_the_scene()
    friday = result.sessions[2]
    # The lag leg took a starting price and waited; without its cancellation it never ended, which the replay cannot do.
    cancelled = next(e for e in friday.outcome.events if e.kind is EventKind.LEG_CANCELLED and e.leg_id == "lag")
    broken = replace(
        result,
        sessions=(
            *result.sessions[:2],
            replace(
                friday,
                outcome=replace(friday.outcome, events=tuple(e for e in friday.outcome.events if e is not cancelled)),
            ),
        ),
    )

    with pytest.raises(SessionRecordInconsistent, match="lag"):
        await session_view_of(a_session_record(broken, 2))
