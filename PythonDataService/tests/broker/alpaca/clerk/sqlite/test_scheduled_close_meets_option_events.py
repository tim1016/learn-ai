"""Where a scheduled close meets what ends an option position with no order of the Clerk's (#2913).

Two rules of the Clerk meet on one held contract. It closes the position by
itself when its close comes due (ADR 0076 decision 20), and it books what
ended the position with no order of its own: an expiry, a sale by the
broker, a Dry Run's settlement, the owner's ending (decision 22). Each test
lets both happen to one bot and asserts what the owner would see.

The scene is the one both rules' own tests use: one SPY 585 call expiring
Friday 2026-09-18, bought at $1.94 a share. Its close is due at 15:15 New
York time and its last session closes at 16:00.

The Paper account is the harness's: the real Clerk on a venue double. The
Dry Run is the real Clerk of a ``sim:`` account. Nothing here was sent to
Alpaca or recorded from it.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
from app.broker.alpaca.clerk.sqlite import deadline_keeper as keeper_module
from app.broker.alpaca.clerk.sqlite import exit_watchdog, option_events
from app.broker.alpaca.clerk.sqlite.deadline_keeper import DeadlineKeeper
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.broker.alpaca.clerk.sqlite.exit_watchdog import BrokerSymbolView
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.facts import ScheduledCloseFacts
from app.broker.alpaca.clerk.sqlite.projections import read_recovery_context
from app.broker.alpaca.clerk.sqlite.recovery_execution import RecoveryExecutionRequest, execute_recovery_action
from app.broker.alpaca.clerk.sqlite.recovery_policy import RecoveryActionUnavailableError, RecoveryPolicyContext
from app.broker.alpaca.clerk.sqlite.repository import ExecutionLeaseLost
from app.broker.alpaca.clerk.sqlite.scheduled_close import CloseStanding, scheduled_close_state
from app.broker.alpaca.clerk.sqlite.uncertainty import open_exit_stuck
from app.broker.contract.errors import BrokerOrderRejected
from tests.broker.alpaca.clerk.sqlite.conftest import _broker_order_fixture
from tests.broker.alpaca.clerk.sqlite.option_event_fixtures import bookings, books_at, fifo, holds_option
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    SYMBOL,
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.sqlite.test_closing_clerk_resting_entries import _open_the_books_again, _the_clerk
from tests.broker.alpaca.clerk.sqlite.test_dry_run_option_expiry import _booked, _keeps_spy
from tests.broker.alpaca.clerk.sqlite.test_option_close_fences import _hand_over, _page_notices, _wait_until
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import (  # noqa: F401
    SID,
    _DryRun,
    dry_run,
)
from tests.broker.alpaca.clerk.sqlite.test_option_event_rounds import CALL as AAPL_CALL
from tests.broker.alpaca.clerk.sqlite.test_option_event_rounds import THE_DAY_BEFORE
from tests.broker.alpaca.clerk.sqlite.test_option_events_on_an_account import SATURDAY_NOON, _expiry_row, _outside
from tests.broker.alpaca.clerk.sqlite.test_option_owner_ending import MONDAY_CLOSE, _offered, _press
from tests.broker.alpaca.clerk.sqlite.test_scheduled_close import (
    CLOSE,
    DUE,
    PASS_MS,
    SELLS_ONE_TO_CLOSE,
    _a_holds_a_call_and_bs_entry_rests_past_its_wait,
    _closes_sent,
    _holds_one_call,
    _notice,
    _until,
)
from tests.broker.alpaca.clerk.sqlite.test_scheduled_close_as_the_clerk_closes import _the_next_start

WRITE_OFF = "discharge_attributed_residue"
MISSED, NOT_FLAT, NOT_SETTLED, NOT_OURS = (
    "SCHEDULED_CLOSE_MISSED", "EXIT_NOT_FLAT", "OPTION_EXPIRED_NOT_SETTLED", "OPTION_ORDER_NOT_OURS"
)


def _open_notices(account: PaperOptions, sid: str = "a") -> set[str]:
    """The reason of every notice open on the bot."""
    return {row["reason_code"] for row in account.repo.active_uncertainties() if row["strategy_instance_id"] == sid}


async def _a_close_alpaca_refused_until_the_session_ended(account: PaperOptions) -> int:
    """Bot ``a``'s close is taken a minute before the close and refused, and so is its retry. The session ends.

    Returns how many orders the venue had been asked to take by then.
    """
    await _holds_one_call(account)
    _until(account, CLOSE - 60_000)
    account.venue.option_submit_error = BrokerOrderRejected("the venue refused the close", broker="alpaca")
    await account.round()
    account.wait(30_000)
    await account.pass_()
    asked = len(account.venue.option_submit_calls)
    assert asked == 3  # the entry, the close, and one retry
    _until(account, CLOSE)
    await account.pass_()
    account.venue.option_submit_error = None
    return asked


async def _gone_at_alpaca_on_saturday_with_no_record(account: PaperOptions) -> None:
    """Alpaca has taken the expired contract off the account and has published nothing about how it ended."""
    _until(account, SATURDAY_NOON)
    account.account.settled.add(SYMBOL)
    assert (await account.pass_()).verdict == "position_drift"


# ── a close that failed, and the contract then expired unsold ────────────


async def test_a_close_refused_until_its_session_ended_is_told_as_missed_and_alpacas_record_of_the_expiry_ends_it(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """One call bought at $1.94. Its close is refused twice and the session ends: $194.00 is lost, by an expiry.

    The Clerk says three things, each true: a close failed and the contract
    expired, its own deadline passed, and the books still hold what Alpaca
    no longer does. It sends nothing more and does not give the bot up.
    Alpaca's record of the expiry is then read, and one pass books it and
    ends all three.
    """
    asked = await _a_close_alpaca_refused_until_the_session_ended(paper)
    assert _open_notices(paper) == {NOT_FLAT, MISSED}

    await _gone_at_alpaca_on_saturday_with_no_record(paper)

    assert _open_notices(paper) == {NOT_FLAT, MISSED, NOT_SETTLED}
    assert open_exit_stuck(paper.repo, "a") is None and paper.held() == {SYMBOL: 1.0}
    [told] = scheduled_close_state(paper.repo, "a")
    assert told.standing is CloseStanding.MISSED

    paper.account.option_events.rows = [_expiry_row()]
    paper.wait(option_events.OPTION_EVENT_POLL_RETRY_MS)
    after = await paper.pass_()

    assert after.verdict == "clean" and paper.held() == {}
    assert [booked["summary_code"] for booked in bookings(paper.repo)] == ["OPTION_EXPIRED"]
    assert fifo(paper.repo, "a").exact_realized_pnl == Decimal("-194")
    assert _open_notices(paper) == set() and scheduled_close_state(paper.repo, "a") == ()
    # Nothing was sent after the session ended: not for the expired contract, and not once it was gone.
    assert len(paper.venue.option_submit_calls) == asked


async def _press_the_write_off(account: PaperOptions, sid: str = "a") -> Any:
    """The owner presses the write-off as the bot's page shows it now."""
    shown = await _offered(account.clerk, sid, WRITE_OFF)

    async def current_context() -> RecoveryPolicyContext:
        context = await read_recovery_context(account.clerk, strategy_instance_id=sid)
        assert context is not None
        return context

    return await execute_recovery_action(
        account.clerk,
        request=RecoveryExecutionRequest(
            action_id=WRITE_OFF, concurrency_token=shown.concurrency_token, execution_ref=shown.execution_ref,
            reason="expired worthless", operator="the-owner",
        ),
        current_context=current_context,
    )


async def test_under_a_missed_close_an_expired_contract_is_never_written_off_and_the_owner_ends_it_at_their_word(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """A missed close and a failed exit are both notices a write-off is offered under. For an expired contract
    it is not: a write-off would leave the $194.00 on the books as a position for good. No record of the
    expiry ever comes, so after Monday's close the owner ends the contract, and the $194.00 is the loss."""
    await _a_close_alpaca_refused_until_the_session_ended(paper)
    await _gone_at_alpaca_on_saturday_with_no_record(paper)

    write_off = await _offered(paper.clerk, "a", WRITE_OFF)

    assert not write_off.available and not write_off.needed
    assert write_off.unavailable_reason_code == "EXPIRED_OPTION_CONTRACT_IS_NOT_WRITTEN_OFF"
    with pytest.raises(RecoveryActionUnavailableError):
        await _press_the_write_off(paper)
    assert paper.held() == {SYMBOL: 1.0}

    _until(paper, MONDAY_CLOSE)
    ended = await _press(paper.clerk, "a")

    assert ended.applied and paper.held() == {}
    assert [booked["summary_code"] for booked in bookings(paper.repo)] == ["OPTION_ENDED_BY_OWNER"]
    assert fifo(paper.repo, "a").exact_realized_pnl == Decimal("-194") and fifo(paper.repo, "a").open_lots == []
    # The missed close ends on the next round, which finds nothing held, and a pass ends the rest.
    await paper.round()
    assert _notice(paper) is None
    assert (await paper.pass_()).verdict == "clean" and _open_notices(paper) == set()


async def test_a_dry_runs_close_with_nothing_bid_is_tried_until_the_session_ends_and_its_simulation_then_settles_it(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """A running Dry Run holds one call bought at $1.94. Nothing is bid in its last 45 minutes.

    The close is taken at 15:15 and again thirty seconds apart, and sells
    nothing for nothing. At the close the keeper records the close as
    missed and asks the simulation, which is still waiting for the closing
    bar. The bar lands at 590.25: the contract is sold by the simulation's
    rule at 5.25 a share, $525.00, and $331.00 is made. The missed close
    ends on the round after that.
    """
    runtime = get_clerk_runtime(dry_run.repo.account_id)
    assert runtime is not None and isinstance(runtime.deadline_keeper, DeadlineKeeper)
    keeper = runtime.deadline_keeper

    async def a_round() -> tuple[Any, Any]:
        done = await keeper.tick()
        return done, await keeper.option_events_read()

    await dry_run.enter(dry_run.evidence())
    _wait_until(dry_run, DUE)
    tries: list[str] = []
    for _try in range(3):
        _hand_over(dry_run, "0", "0.05")
        done, _read = await a_round()
        tries.extend(done.closes.taken)
        dry_run.wait(30_000)

    first = tries[0]
    assert tries == [first, f"{first}-again-1", f"{first}-again-2"] and "scheduled-close-20260918-" in first
    assert set(await _page_notices(dry_run)) == {NOT_FLAT} and dry_run.held() == {SYMBOL: 1.0}

    _wait_until(dry_run, CLOSE + 500)
    at_the_close, read = await a_round()

    # No close is taken once the session has closed. The simulation was asked and is waiting for its bar,
    # so nothing is booked and it holds the bot for nothing yet.
    assert at_the_close.closes.taken == () and at_the_close.closes.missed == ((SID, SYMBOL),)
    assert read is not None and read.read and _booked(dry_run) == []
    assert set(await _page_notices(dry_run)) == {NOT_FLAT, MISSED}

    _keeps_spy(dry_run, "590.25")
    dry_run.wait(option_events.OPTION_EVENT_POLL_RETRY_MS)
    await a_round()

    assert [booked["summary_code"] for booked in _booked(dry_run)] == ["OPTION_SETTLED_IN_CASH"]
    assert dry_run.held() == {} and dry_run.realized_pnl() == Decimal("331")
    # The Clerk's own closes sold nothing: the one sale is the simulation's settlement.
    assert [(side, price) for _symbol, side, _contracts, price, _multiplier in dry_run.fills()] == [
        ("buy", 1.94), ("sell", 5.25)
    ]

    dry_run.wait(keeper_module.IDLE_WAKE_MS)
    after, _read = await a_round()

    assert after.closes.taken == () and after.closes.missed == ()
    assert MISSED not in set(await _page_notices(dry_run))
    assert open_exit_stuck(dry_run.repo, SID) is None
    # The failed close's own notice ends on a flat proof, which a pass makes. A Dry Run makes no pass on a timer.
    await dry_run.clerk.reconcile_once()
    assert await _page_notices(dry_run) == {}


# ── a close that comes due beside the broker's own sale ──────────────────


async def test_a_due_close_is_taken_and_not_sent_beside_alpacas_own_sale_and_ends_once_that_sale_is_booked(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Alpaca is selling the bot's one call as its close comes due. Two orders would be selling one contract.

    The close is taken and kept, and no order of the Clerk's goes out,
    from the keeper or from any pass. It is no failed close: nothing is
    retried and the bot is not given up. Alpaca's sale fills at $0.40 and
    is booked against the bot, and the close that was kept ends with
    nothing left to sell.
    """
    await _holds_one_call(paper)
    _until(paper, DUE - 60_000)
    paper.account.outside_orders = [_outside("alpaca-sells-1", status="new", symbol=SYMBOL)]
    await paper.pass_()
    assert _open_notices(paper) == {NOT_OURS}
    asked_before = list(paper.venue.option_submit_calls)

    _until(paper, DUE)
    at_its_time = await paper.round()

    [taken] = at_its_time.closes.taken
    assert (at_its_time.closes.driven, at_its_time.closes.deferred) == ((), (taken,))
    for _ in range(4):  # a minute of passes, with a round each
        paper.wait(PASS_MS)
        await paper.pass_()
        assert (await paper.round()).closes.taken == ()
    assert paper.venue.option_submit_calls == asked_before and _closes_sent(paper) == []
    assert _open_notices(paper) == {NOT_OURS} and open_exit_stuck(paper.repo, "a") is None
    kept = paper.repo.active_exit_for_strategy("a")
    assert kept is not None and kept.effect_operation_id == taken

    # Alpaca's stream reports the sale filled, and the account no longer holds the contract.
    observe_external_order(
        paper.repo, order=_outside("alpaca-sells-1", status="filled", filled=1, price=0.40, symbol=SYMBOL)
    )
    paper.account.outside_orders = []
    paper.account.settled.add(SYMBOL)
    for _ in range(2):
        paper.wait(PASS_MS)
        await paper.pass_()

    assert paper.held() == {} and _open_notices(paper) == set()
    assert [booked["summary_code"] for booked in bookings(paper.repo)] == ["OPTION_SOLD_OUTSIDE"]
    assert paper.repo.active_exit_for_strategy("a") is None
    assert paper.venue.option_submit_calls == asked_before
    # The session then ends with nothing held: no close was missed.
    _until(paper, CLOSE + 1_000)
    await paper.round()
    assert (await paper.pass_()).verdict == "clean" and _open_notices(paper) == set()


def test_a_retry_of_a_failed_close_waits_while_the_clerk_closes_and_beside_alpacas_sale_it_names_the_sale(
    tmp_path: Path,
) -> None:
    """Two things can hold the Clerk's retry of a failed scheduled close before anything is accepted.

    A closing Clerk sends no failed close again. An order the Clerk did
    not place in the bot's contract keeps every order of the bot's own
    back. Each is a wait and no failure. When both stand, the owner is told
    of the order: it is the one that lasts, and the one that names what to
    look at.
    """
    repo = books_at(tmp_path, THE_DAY_BEFORE, {"bot-a": "AAPL"})
    holds_option(repo, "bot-a", AAPL_CALL, contracts=1)
    the_account_agrees = BrokerSymbolView(broker_qty=1.0, attributed_qty=1.0, working=False, agrees=True)
    under_its_own_rule = ScheduledCloseFacts(kind="expiry_day_close", due_ms=repo.clock(), session_open_ms=repo.clock())

    def retry() -> Any:
        return exit_watchdog._accept_admissible_redrive(
            repo, broker_symbol=lambda _symbol: the_account_agrees, strategy_instance_id="bot-a", symbol=AAPL_CALL,
            decision_id="redrive-1", entry_order_ref="unused", confirmed_shape=None,
            scheduled_close=under_its_own_rule,
        )

    try:
        repo.begin_closing()
        while_closing = retry()
        observe_external_order(repo, order=_broker_order_fixture(
            "not-ours-1", order_id="alpaca-sells-1", symbol=AAPL_CALL, status="new", side="sell", quantity=1,
        ))
        beside_the_sale = retry()

        assert (while_closing.outcome, while_closing.reason_code) == ("hold", "RECOVERY_CLERK_CLOSING")
        assert (beside_the_sale.outcome, beside_the_sale.reason_code) == ("hold", NOT_OURS)
        assert "alpaca-sells-1" in (beside_the_sale.explanation or "")
    finally:
        repo.close()


# ── one round of the keeper ──────────────────────────────────────────────


class _SlowOptionEvents:
    """The account's read of option events, answered only when the test says so. Counts how often it was asked."""

    def __init__(self, account: PaperOptions) -> None:
        self.asked = 0
        self.answers = asyncio.Event()
        self._events = account.account.option_events
        self._real = self._events.read_option_event_activities
        self._events.read_option_event_activities = self._slow  # type: ignore[method-assign]

    async def _slow(self, **asked_for: Any) -> Any:
        self.asked += 1
        await self.answers.wait()
        return await self._real(**asked_for)

    def answered_as_it_was(self) -> None:
        """Put the account's own read back, and let any read still out be answered."""
        self._events.read_option_event_activities = self._real  # type: ignore[method-assign]
        self.answers.set()


async def test_the_round_starts_its_read_of_option_events_only_once_a_due_close_has_been_sent(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """A close that is due comes first. While Alpaca has not answered its order the round asks for no events."""
    resting = await _a_holds_a_call_and_bs_entry_rests_past_its_wait(paper)
    events = _SlowOptionEvents(paper)
    events.answers.set()
    being_sent, alpaca_answers = asyncio.Event(), asyncio.Event()
    real_send = paper.venue.submit_option_order

    async def send_that_alpaca_is_slow_to_answer(order: object, *, client_order_id: str) -> object:
        being_sent.set()
        await alpaca_answers.wait()
        return await real_send(order, client_order_id=client_order_id)  # type: ignore[arg-type]

    paper.venue.submit_option_order = send_that_alpaca_is_slow_to_answer  # type: ignore[method-assign]
    keeper = paper.another_keeper(read=paper.account)
    the_round = asyncio.create_task(keeper.tick())
    try:
        await asyncio.wait_for(being_sent.wait(), timeout=5)
        for _ in range(5):
            await asyncio.sleep(0)
        # The entry past its wait is already cancelled, the close is at Alpaca, and no read has begun.
        assert paper.outcome(resting).standing is EntryStanding.UNFILLED and events.asked == 0

        alpaca_answers.set()
        done = await asyncio.wait_for(the_round, timeout=5)
        read = await keeper.option_events_read()

        assert done.failed == () and done.entries.ended == (resting,) and len(done.closes.taken) == 1
        assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]
        assert read is not None and read.read and events.asked == 1
    finally:
        alpaca_answers.set()
        the_round.cancel()
        await keeper.stop()


async def test_a_read_of_option_events_that_alpaca_has_not_answered_holds_up_no_close(
    paper: PaperOptions,  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, DUE - 60_000)
    events = _SlowOptionEvents(paper)
    keeper = paper.another_keeper(read=paper.account)
    try:
        await asyncio.wait_for(keeper.tick(), timeout=5)
        await asyncio.sleep(0)
        assert events.asked == 1 and _closes_sent(paper) == []

        _until(paper, DUE)
        done = await asyncio.wait_for(keeper.tick(), timeout=5)

        # The close went out with the read still out, and no second read was begun beside it.
        assert len(done.closes.taken) == 1 and _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]
        assert events.asked == 1
    finally:
        events.answered_as_it_was()
        await keeper.stop()


async def test_a_close_step_that_breaks_does_not_keep_the_round_from_reading_option_events(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, SATURDAY_NOON)
    paper.account.settled.add(SYMBOL)
    paper.account.option_events.rows = [_expiry_row()]

    async def breaks(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("the close step cannot read what is held")

    monkeypatch.setattr(keeper_module, "drive_scheduled_closes", breaks)
    keeper = paper.another_keeper(read=paper.account)
    try:
        done = await keeper.tick()
        read = await keeper.option_events_read()
    finally:
        await keeper.stop()

    assert done.failed == ("scheduled_closes",)
    assert read is not None and read.read and paper.held() == {}
    assert [booked["summary_code"] for booked in bookings(paper.repo)] == ["OPTION_EXPIRED"]


async def test_a_lost_hold_on_the_account_in_the_close_step_ends_the_round_before_any_read_of_option_events(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Nothing more can be written under a lost hold, so the round asks Alpaca for nothing it would have to book."""
    await _holds_one_call(paper)
    _until(paper, SATURDAY_NOON)
    paper.account.option_events.rows = [_expiry_row()]
    asked_before = len(paper.account.option_events.reads)

    async def loses_the_account(*_args: object, **_kwargs: object) -> object:
        raise ExecutionLeaseLost("another Clerk holds the account")

    monkeypatch.setattr(keeper_module, "drive_scheduled_closes", loses_the_account)
    keeper = paper.another_keeper(read=paper.account)
    try:
        with pytest.raises(ExecutionLeaseLost):
            await keeper.tick()
        assert await keeper.option_events_read() is None
    finally:
        await keeper.stop()

    assert len(paper.account.option_events.reads) == asked_before and bookings(paper.repo) == []


# ── a Clerk that closes ──────────────────────────────────────────────────


async def test_a_closing_clerk_takes_no_close_ends_the_read_its_keeper_had_out_and_the_next_start_does_both(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Bot a's close is due, bot b's entry rests, and Alpaca has not answered the keeper's read of option events.

    The Clerk closes. It cancels the resting entry and proves it, takes no
    close, and does not wait for the read: a broker that does not answer
    must not hold the shutdown. The next start sends the close once and
    reads the account's option events.
    """
    await _holds_one_call(paper, "a")
    paper.wait(1_000)
    paper.account.account_observed_at_ms = paper.clock()
    assert await paper.sync.tick() == "observed"
    resting = await paper.resting("b")
    events = _SlowOptionEvents(paper)
    paper.keeper = paper.another_keeper(read=paper.account)
    await paper.keeper.tick()
    await asyncio.sleep(0)
    assert events.asked == 1
    _until(paper, DUE)

    await asyncio.wait_for(_the_clerk(paper).close(), timeout=5)
    events.answered_as_it_was()
    for _ in range(5):
        await asyncio.sleep(0)

    _open_the_books_again(paper)
    assert _closes_sent(paper) == [] and paper.repo.active_exit_for_strategy("a") is None
    assert paper.outcome(resting).standing is EntryStanding.UNFILLED
    assert events.asked == 1 and paper.account.option_events.reads == []

    await _the_next_start(paper)

    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]
    assert len(paper.account.option_events.reads) == 1
