"""What a closer look at a saved run decides from the headers it has read, with no database behind it (#3030).

The reads themselves (counts, pages, the session rows) run against a real Postgres in ``test_run_detail_reads.py``. The
decisions made on what they return need none: is the run there and complete, is the leg one its setup has, may these two
runs be compared. They are functions of the headers, built here from the same dataclasses the store fills, so a pull
request, which has no database, holds them. The order of the refusals is the same everywhere: the run missing, the run not
complete, then the session or the leg. For two runs: the first missing, every one not complete, every one whose setup is
lost, then the underlyings.
"""

from __future__ import annotations

import pytest

from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.run_detail import (
    LegNotInRun,
    RunNotComplete,
    RunNotFound,
    RunsNotComplete,
    RunsOfDifferentUnderlyings,
    RunsSetupCannotBeRead,
    TradePage,
    judge_pair,
    require_a_complete_run,
    require_the_leg,
)
from app.options_backtest.saved_runs import PresentedStatus, StoredTrade
from tests.options_backtest._detail_scene import SESSIONS, WED_OPEN_MS, the_scene, the_setup
from tests.options_backtest._headers import THE_REASON, a_header, an_unreadable_header
from tests.options_backtest._run_data import fake_data


@pytest.fixture(scope="module")
def scene() -> RunResult:
    return run_sessions(the_setup(), SESSIONS, fake_data(the_scene()))


# ── One run ──────────────────────────────────────────────────────────────


def test_a_run_that_is_missing_is_refused_as_missing_and_one_that_is_complete_is_handed_back() -> None:
    header = a_header(7)

    assert require_a_complete_run(7, header) is header
    with pytest.raises(RunNotFound) as caught:
        require_a_complete_run(7, None)
    assert (caught.value.run_id, caught.value.role) == (7, None)


@pytest.mark.parametrize("status", ["running", "failed", "cancelled", "interrupted"])
def test_a_run_that_is_not_complete_is_refused_naming_the_status_it_has(status: PresentedStatus) -> None:
    with pytest.raises(RunNotComplete) as caught:
        require_a_complete_run(7, a_header(7, status=status))

    assert (caught.value.run_id, caught.value.status) == (7, status)
    assert status in str(caught.value)


def test_a_leg_the_setup_has_is_let_through_one_it_lacks_is_refused_and_no_leg_asks_nothing() -> None:
    header = a_header(7)  # the base setup has one leg, "call"

    require_the_leg(header, "call")
    require_the_leg(header, None)
    with pytest.raises(LegNotInRun) as caught:
        require_the_leg(header, "put")
    assert (caught.value.run_id, caught.value.leg_id) == (7, "put")


def test_with_no_setup_to_name_its_legs_a_leg_is_let_through_for_the_stored_trades_to_answer() -> None:
    # The setup is lost, so there is no way to say "this run has no such leg"; the page of trades holds what was stored.
    lost = an_unreadable_header(7)

    require_the_leg(lost, "call")
    require_the_leg(lost, "nothing_of_the_kind")


# ── A page of trades ─────────────────────────────────────────────────────


def _page(scene: RunResult, *, total: int, offset: int, taken: int) -> TradePage:
    trades = tuple(
        StoredTrade(WED_OPEN_MS, number, trade) for number, trade in enumerate(scene.sessions[0].outcome.trades)
    )
    return TradePage(run_id=7, total=total, offset=offset, limit=2, trades=trades[:taken])


def test_the_next_page_begins_where_this_one_ended_and_there_is_none_after_the_last_trade(scene: RunResult) -> None:
    assert len(scene.sessions[0].outcome.trades) == 3, "the scene's Wednesday holds three trades"

    # Of 3 trades: a page of 2 from the start ends at 2; a page of 1 from 2 reaches the 3rd; a page of 3 from 0 does too.
    assert _page(scene, total=3, offset=0, taken=2).next_offset == 2
    assert _page(scene, total=3, offset=2, taken=1).next_offset is None
    assert _page(scene, total=3, offset=0, taken=3).next_offset is None
    # Past the end: nothing came back, nothing is left to ask for.
    assert _page(scene, total=3, offset=5, taken=0).next_offset is None
    assert _page(scene, total=0, offset=0, taken=0).next_offset is None


# ── Two runs ─────────────────────────────────────────────────────────────


def test_the_first_run_missing_is_the_one_named_and_it_is_named_by_its_place() -> None:
    there = a_header(2)

    with pytest.raises(RunNotFound) as first:
        judge_pair(1, None, 2, there)
    with pytest.raises(RunNotFound) as second:
        judge_pair(1, there, 2, None)
    with pytest.raises(RunNotFound) as both:
        judge_pair(1, None, 2, None)

    assert (first.value.run_id, first.value.role) == (1, "a")
    assert (second.value.run_id, second.value.role) == (2, "b")
    assert (both.value.run_id, both.value.role) == (1, "a")


def test_every_run_that_is_not_complete_is_named_with_its_place_and_its_status() -> None:
    complete = a_header(1)

    with pytest.raises(RunsNotComplete) as only_b:
        judge_pair(1, complete, 2, a_header(2, status="failed"))
    with pytest.raises(RunsNotComplete) as only_a:
        judge_pair(1, a_header(1, status="cancelled"), 2, complete)
    with pytest.raises(RunsNotComplete) as both:
        judge_pair(1, a_header(1, status="running"), 2, a_header(2, status="interrupted"))

    assert only_b.value.runs == (("b", 2, "failed"),)
    assert only_a.value.runs == (("a", 1, "cancelled"),)
    assert both.value.runs == (("a", 1, "running"), ("b", 2, "interrupted"))


def test_every_run_whose_stored_setup_is_lost_is_named_with_its_place_and_the_reason_beside_it() -> None:
    readable = a_header(1)

    with pytest.raises(RunsSetupCannotBeRead) as only_b:
        judge_pair(1, readable, 2, an_unreadable_header(2))
    with pytest.raises(RunsSetupCannotBeRead) as both:
        judge_pair(1, an_unreadable_header(1, "leg: gone"), 2, an_unreadable_header(2, "slippage: gone"))

    assert only_b.value.runs == (("b", 2, THE_REASON),)
    assert str(only_b.value) == f"the stored setup of run 2 (b) can no longer be read: {THE_REASON}"
    assert both.value.runs == (("a", 1, "leg: gone"), ("b", 2, "slippage: gone"))


def test_runs_that_trade_different_underlyings_are_refused_naming_each_with_its_own() -> None:
    spy, qqq = a_header(1, underlying="SPY"), a_header(2, underlying="QQQ")

    with pytest.raises(RunsOfDifferentUnderlyings) as caught:
        judge_pair(1, spy, 2, qqq)

    assert (caught.value.a, caught.value.b) == ((1, "SPY"), (2, "QQQ"))


def test_the_reasons_are_given_in_one_order_not_complete_then_a_lost_setup_then_the_underlyings() -> None:
    # Run 1 failed, and run 2 has lost its setup and trades another underlying: the run that is not complete comes first.
    failed, lost = a_header(1, status="failed"), an_unreadable_header(2, underlying="QQQ")
    with pytest.raises(RunsNotComplete):
        judge_pair(1, failed, 2, lost)
    # With both complete, the lost setup comes before the underlyings, whose difference is only known from the record.
    with pytest.raises(RunsSetupCannotBeRead):
        judge_pair(1, a_header(1), 2, lost)


def test_the_same_run_twice_is_a_pair_that_may_be_compared_with_itself() -> None:
    header = a_header(1)

    assert judge_pair(1, header, 1, header) == (header, header)
    assert judge_pair(1, header, 2, a_header(2)) == (header, a_header(2))
