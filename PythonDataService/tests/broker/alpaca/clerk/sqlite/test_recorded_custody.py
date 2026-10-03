"""What a bot holds, by its store's own records: each arm of the proof (#2694).

A graduated Shadow store is never reconciled again, so Clear proves a
rehearsal bot holds nothing from its records alone. Each test leaves one thing
in a store and reads back what the proof names; the rehearsal that entered,
exited and ended flat -- a filled ENTER left ``in_progress`` -- is pinned end
to end in ``test_duty_settle``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.recorded_custody import RecordedCustody, read_recorded_custody
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_account_hold, raise_uncertainty
from app.broker.contract.errors import BrokerUnavailable
from tests.broker.alpaca.clerk.sqlite.conftest import (
    _broker_leg,
    _clock_at,
    _FakeTradePort,
    _make_held_position,
)

ACCOUNT_ID = "shadow:9LIVE0001"
SID = "spy-bot"
OTHER_SID = "qqq-bot"
RUN_ID = "run-1"


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    """A store with ``SID`` running and ``OTHER_SID`` beside it."""
    store = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_at(1_700_000_000_000)
    )
    store.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    store.register_strategy_instance(strategy_instance_id=OTHER_SID, symbol="QQQ", config_hash="h2")
    submit_start_run(store, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    yield store
    store.close()


def _stop(repo: ClerkSqliteRepository) -> None:
    submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)


def _read(repo: ClerkSqliteRepository, *, open_book_orders: int = 0) -> RecordedCustody:
    return read_recorded_custody(repo, SID, open_book_orders=open_book_orders)


def _problem(repo: ClerkSqliteRepository, sid: str) -> None:
    raise_uncertainty(
        repo, strategy_instance_id=sid, reason_code="ORDER_OUTCOME_UNKNOWN",
        headline="h", explanation="e", operator_impact="oi", next_step="ns",
    )


def test_a_stopped_bot_that_never_traded_holds_nothing(repo: ClerkSqliteRepository) -> None:
    _stop(repo)

    custody = _read(repo)

    assert custody.holds_nothing
    assert custody.held_phrase == ""


def test_a_run_the_store_still_holds_active_is_a_holding(repo: ClerkSqliteRepository) -> None:
    custody = _read(repo)

    assert not custody.holds_nothing
    assert custody.held_phrase == "a run that has not ended"


async def test_an_attributed_position_is_named_with_its_symbol_and_quantity(repo: ClerkSqliteRepository) -> None:
    await _make_held_position(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, run_id=RUN_ID)
    _stop(repo)

    custody = _read(repo)

    assert custody.positions == {"SPY": 10.0}
    assert custody.held_phrase == "10 SPY"


async def test_an_accepted_order_the_broker_never_filled_is_a_working_order(repo: ClerkSqliteRepository) -> None:
    await submit_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="enter-1",
        lifecycle_run_id=RUN_ID, leg=_broker_leg(), trade=_FakeTradePort(),
    )
    _stop(repo)

    custody = _read(repo)

    assert (custody.working_orders, custody.unresolved_effects) == (1, 1)
    assert custody.held_phrase == "1 working order and 1 order still waiting on the broker's record"


async def test_an_order_whose_answer_was_lost_is_an_unknown_outcome(repo: ClerkSqliteRepository) -> None:
    """The submit timed out and the broker has no such order yet: it may still land."""
    await submit_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="enter-1",
        lifecycle_run_id=RUN_ID, leg=_broker_leg(),
        trade=_FakeTradePort(submit_error=BrokerUnavailable("timed out", broker="alpaca"), lookup_absent=True),
    )
    _stop(repo)

    custody = _read(repo)

    assert (custody.unknown_outcome_orders, custody.working_orders) == (1, 0)
    assert "1 order whose outcome is unknown" in custody.held_phrase


@pytest.mark.parametrize("scope", ["its own", "the whole account's"])
def test_an_open_custody_problem_is_a_holding_whether_it_names_the_bot_or_the_account(
    scope: str, repo: ClerkSqliteRepository,
) -> None:
    """On an installed account an account-wide problem says nothing about one
    bot, because a later pass can resolve it. Here nothing ever will."""
    _stop(repo)
    if scope == "its own":
        _problem(repo, SID)
    else:
        raise_account_hold(repo, reason_code="UNEXPLAINED_ORDER_HOLD", evidence_refs=["bo-1"])

    custody = _read(repo)

    assert custody.open_uncertainties == 1
    assert custody.held_phrase == "1 unresolved custody problem"


def test_another_bots_custody_problem_is_not_this_bots_holding(repo: ClerkSqliteRepository) -> None:
    _stop(repo)
    _problem(repo, OTHER_SID)

    assert _read(repo).holds_nothing


def test_an_order_the_shadow_book_still_holds_open_is_a_holding(repo: ClerkSqliteRepository) -> None:
    """The second witness: the store's own records show nothing, the book disagrees."""
    _stop(repo)

    custody = _read(repo, open_book_orders=2)

    assert not custody.holds_nothing
    assert custody.held_phrase == "2 simulated orders still open in the Shadow order book"


def test_every_holding_is_named_in_one_phrase() -> None:
    custody = RecordedCustody(
        positions={"SPY": 10.0, "QQQ": -2.5},
        working_orders=2,
        unknown_outcome_orders=0,
        unresolved_effects=0,
        open_uncertainties=1,
        run_active=True,
        open_book_orders=0,
    )

    assert custody.held_phrase == (
        "2.5 QQQ short, 10 SPY, 2 working orders, 1 unresolved custody problem and a run that has not ended"
    )
