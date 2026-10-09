"""A stock entry that failed, whose order Alpaca later shows working (#2961).

A stock bot's ENTER is recorded as failed — its send's answer was lost and
the exact lookup then said the order was not there — and Alpaca later shows
that order as working while the bot's run is still ACTIVE. The tests here
pin the two halves of the fix: the reconciliation pass's step cancels the
order, and the bot's next entry is refused meanwhile.

Nothing here was sent to Alpaca. The venue is a double that took the order,
lost the answer, and holds it working until a cancel ends it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite import failed_enter_entries, reads, stopped_run_entries
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import resolve_enter_submission, submit_enter
from app.broker.alpaca.clerk.sqlite.failed_enter_entries import (
    cancel_working_entries_of_failed_enters,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import (
    fold_order_acknowledgement,
    submit_absence_grace_ms,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    ENTRY_ORDER_WORKING_REASON_CODE,
    admit_new_exposure,
)
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at, _FakeTradePort, _walk_clock_to

ACCOUNT_ID = "PA-FAILED-ENTER"
SID = "spy-bot"
OTHER = "qqq-bot"
RUN_ID = "run-1"


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    clock = _clock_at(1_700_000_000_000)
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    r.register_strategy_instance(strategy_instance_id=OTHER, symbol="QQQ", config_hash="h2")
    submit_start_run(r, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    yield r
    r.close()


def _leg() -> BrokerOrderLeg:
    return BrokerOrderLeg(symbol="SPY", side="buy", quantity=10)


def _working_order(order_ref: str, *, at_ms: int) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca",
        order_id=f"bo-{order_ref}",
        client_order_id=order_ref,
        symbol="SPY",
        asset_class="us_equity",
        side="buy",
        order_type="market",
        time_in_force="day",
        quantity=10.0,
        filled_quantity=0.0,
        limit_price=None,
        stop_price=None,
        filled_avg_price=None,
        status="new",
        submitted_at_ms=at_ms,
        created_at_ms=at_ms,
        updated_at_ms=at_ms,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=at_ms,
    )


class _VenueThatTookTheOrder:
    """A broker that took the stock order and lost the answer, then shows the order when asked.

    The order lands only when the test puts it in ``taken``, stays working
    until a cancel ends it, and every answer is one ``BrokerOrder`` the real
    read port could have returned. While ``silent`` is set the exact lookup
    answers nothing: a broker whose reads are down.
    """

    def __init__(self) -> None:
        self.taken: dict[str, BrokerOrder] = {}
        self.cancel_calls: list[str] = []
        self.silent = False

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        raise BrokerUnavailable("connection reset before any answer", broker="alpaca")

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return None if self.silent else self.taken.get(client_order_id)

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        for ref, order in self.taken.items():
            if order.order_id == order_id and order.status == "new":
                self.taken[ref] = order.model_copy(update={"status": "canceled"})


async def _an_entry_voided_as_never_sent(repo: ClerkSqliteRepository, venue: _VenueThatTookTheOrder) -> str:
    """The send gets no answer and the broker shows no such order through the grace: the ENTER is voided."""
    sent = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=venue,
    )
    assert sent.order_ref is not None
    _walk_clock_to(repo, repo.clock() + submit_absence_grace_ms() + 1)
    await resolve_enter_submission(repo, order_ref=sent.order_ref, trade=venue)
    assert _enter_state(repo, sent.order_ref) == "failed"
    return sent.order_ref


def _the_abandoned_send_lands(repo: ClerkSqliteRepository, venue: _VenueThatTookTheOrder, order_ref: str) -> None:
    """Alpaca has the order after all (#2342), working, and the stream says so."""
    landed = _working_order(order_ref, at_ms=repo.clock())
    venue.taken[order_ref] = landed
    order = repo.order(order_ref)
    assert order is not None
    fold_order_acknowledgement(repo, effect_operation_id=order.effect_operation_id, order=landed)
    assert _broker_state(repo, order_ref) == "new" and _enter_state(repo, order_ref) == "failed"


def _enter_state(repo: ClerkSqliteRepository, order_ref: str) -> str:
    order = repo.order(order_ref)
    assert order is not None
    effect = repo.effect_operation(order.effect_operation_id)
    assert effect is not None
    return effect.state


def _broker_state(repo: ClerkSqliteRepository, order_ref: str) -> str | None:
    order = repo.order(order_ref)
    assert order is not None
    return order.broker_state


def _cancel_requests(repo: ClerkSqliteRepository, order_ref: str) -> list[str]:
    """Why the Clerk recorded it was asking for a cancel, each time it did."""
    return [
        row["summary_code"]
        for row in repo.transitions_for_order(order_ref)
        if row["transition_kind"] == "ORDER_CANCEL_REQUESTED"
    ]


async def test_a_failed_stock_enters_order_alpaca_later_shows_working_is_cancelled_while_its_run_lives(
    repo: ClerkSqliteRepository,
) -> None:
    venue = _VenueThatTookTheOrder()
    order_ref = await _an_entry_voided_as_never_sent(repo, venue)
    _the_abandoned_send_lands(repo, venue, order_ref)

    # The pass's step owns it however the run stands; the stopped-run step still waits for the run.
    assert [entry.order_ref for entry in failed_enter_entries.entries_owed_a_cancel(repo)] == [order_ref]
    assert stopped_run_entries.entries_owed_a_cancel(repo) == []

    # Meanwhile the bot's next entry is refused beside it, and another bot is not held.
    refused = admit_new_exposure(repo, strategy_instance_id=SID)
    assert refused.reason_code == ENTRY_ORDER_WORKING_REASON_CODE
    assert admit_new_exposure(repo, strategy_instance_id=OTHER).allowed

    await cancel_working_entries_of_failed_enters(repo, trade=venue)

    assert venue.cancel_calls == [f"bo-{order_ref}"]
    assert _broker_state(repo, order_ref) == "canceled"
    assert _cancel_requests(repo, order_ref) == ["ENTRY_ENTER_FAILED"]
    assert failed_enter_entries.entries_owed_a_cancel(repo) == []
    # With the order ended, the bot's next entry is admitted again.
    assert admit_new_exposure(repo, strategy_instance_id=SID).allowed


async def test_once_the_run_has_ended_the_stopped_run_step_owns_the_order_and_this_step_leaves_it(
    repo: ClerkSqliteRepository,
) -> None:
    venue = _VenueThatTookTheOrder()
    order_ref = await _an_entry_voided_as_never_sent(repo, venue)
    _the_abandoned_send_lands(repo, venue, order_ref)
    submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)

    assert failed_enter_entries.entries_owed_a_cancel(repo) == []
    assert [entry.order_ref for entry in stopped_run_entries.entries_owed_a_cancel(repo)] == [order_ref]


async def test_an_entry_under_a_still_open_enter_is_never_this_steps_to_cancel(
    repo: ClerkSqliteRepository,
) -> None:
    """Its own machine — the POST, the resolver, the wait — still owns the order."""
    sent = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-open",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=_FakeTradePort(),
    )
    assert sent.order_ref is not None
    assert _broker_state(repo, sent.order_ref) == "accepted"

    assert failed_enter_entries.entries_owed_a_cancel(repo) == []
    assert stopped_run_entries.entries_owed_a_cancel(repo) == []


async def test_a_cancel_whose_proof_stays_uncertain_is_asked_for_again_by_the_next_pass(
    repo: ClerkSqliteRepository,
) -> None:
    """An uncertain cancel cannot end the asking: the ENTER's failure is terminal and stays.

    The shared uncertain fold never moves a terminal effect back, so the
    order stays on this step's worklist and the next pass asks again — as
    the stopped-run step's orders are — until a lookup proves the order
    ended.
    """
    venue = _VenueThatTookTheOrder()
    order_ref = await _an_entry_voided_as_never_sent(repo, venue)
    _the_abandoned_send_lands(repo, venue, order_ref)

    venue.silent = True
    await cancel_working_entries_of_failed_enters(repo, trade=venue)

    assert venue.cancel_calls == [f"bo-{order_ref}"]
    assert _enter_state(repo, order_ref) == "failed" and _broker_state(repo, order_ref) == "new"
    # Still owed, and the bot's next entry is still refused beside it.
    assert [entry.order_ref for entry in failed_enter_entries.entries_owed_a_cancel(repo)] == [order_ref]
    assert admit_new_exposure(repo, strategy_instance_id=SID).reason_code == ENTRY_ORDER_WORKING_REASON_CODE

    # The broker's reads come back: the next pass asks once more, and the proof lands.
    venue.silent = False
    await cancel_working_entries_of_failed_enters(repo, trade=venue)

    assert venue.cancel_calls == [f"bo-{order_ref}", f"bo-{order_ref}"]
    assert _broker_state(repo, order_ref) == "canceled"
    assert failed_enter_entries.entries_owed_a_cancel(repo) == []
    assert admit_new_exposure(repo, strategy_instance_id=SID).allowed


async def test_the_worklist_is_sought_from_the_entry_state_index_and_touches_no_table_by_a_scan(
    repo: ClerkSqliteRepository,
) -> None:
    """Every pass reads this worklist: it must cost an index seek, not a walk of history."""
    sent = await submit_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-plan",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=_FakeTradePort(),
    )
    assert sent.order_ref is not None and _broker_state(repo, sent.order_ref) == "accepted"

    with repo.write_fence() as conn:
        plan = [
            row[3]
            for row in conn.execute(
                f"EXPLAIN QUERY PLAN {reads.WORKING_SHARE_ENTRIES_UNDER_FAILED_ENTERS_SQL}",
                reads.WORKING_SHARE_ENTRIES_UNDER_FAILED_ENTERS_PARAMS,
            )
        ]

    assert any(
        "ix_orders_entry_broker_state (role=? AND <expr>=? AND effect_state=?)" in step for step in plan
    )
    assert [step for step in plan if step.startswith("SCAN")] == []
