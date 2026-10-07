"""What a Clerk that closes does with a scheduled close, and what the next start finds (#2913).

A closing Clerk sends no new order: its keeper's last round cancels resting
entries and takes no close. But a close taken just before the Clerk began
to close is still being driven, and the stop can cut it anywhere. These
tests cut it in each place and assert what the owner would find afterwards:
the close is not lost, and the venue was never sent a second closing order.

The Clerk is the Paper harness's, held by the runtime ``main.py`` closes.
Nothing here was sent to Alpaca or recorded from it.

The last tests are about a close that is not yet taken when the Clerk
begins to close. The account's reconciliation loop and the keeper's own task
both go on until the shutdown stops them, and either can look in that time.
Their runtime holds the reconciliation loop too, with its timer in the
test's hands.
"""

from __future__ import annotations

import asyncio

import pytest

from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.sqlite import deadline_keeper as keeper_module
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import ReconciliationSweep
from app.broker.alpaca.clerk.sqlite.scheduled_close import (
    SCHEDULED_CLOSE_RETRY_AFTER_MS,
    CloseStanding,
    scheduled_close_state,
)
from app.broker.contract.errors import BrokerOrderRejected
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.sqlite.test_closing_clerk_resting_entries import _open_the_books_again, _the_clerk
from tests.broker.alpaca.clerk.sqlite.test_scheduled_close import (
    DUE,
    PASS_MS,
    SELLS_ONE_TO_CLOSE,
    _closes_sent,
    _closing_refs,
    _comes_true,
    _driving_of,
    _holds_one_call,
    _the_close_fills,
    _two_bots_each_hold_one_call,
    _until,
)


async def _the_next_start(paper: PaperOptions) -> None:  # noqa: F811
    """The new Clerk over the books the closed one left runs its startup recovery and makes its first pass."""
    await paper.clerk.recover()
    paper.wait(PASS_MS)
    await paper.pass_()


class _Timer:
    """A task's sleep, in the test's hands: it wakes only when the test says its timer fired."""

    def __init__(self) -> None:
        self._fired = asyncio.Semaphore(0)
        self.sleeps = 0

    async def sleep(self, _seconds: float, _nudged: asyncio.Event | None = None) -> None:
        self.sleeps += 1
        await self._fired.acquire()

    def fires(self) -> None:
        self._fired.release()


async def _never(_seconds: float) -> None:
    await asyncio.Event().wait()


def _the_clerk_with_its_reconciliation_loop(
    paper: PaperOptions,  # noqa: F811
) -> tuple[ActiveClerkRuntime, _Timer, list[AccountReconciliationResult]]:
    """The account's Clerk as ``main.py`` holds it, its reconciliation loop included, and each pass that loop ended."""
    timer, passes = _Timer(), []
    clerk = _the_clerk(paper)
    clerk.sweep = ReconciliationSweep(
        repo=paper.repo, read=paper.account, trade=paper.venue, intake=paper.clerk.intake,
        pricing=paper.clerk.recovery_pricing, sleep=timer.sleep, lease_sleep=_never, on_result=passes.append,
    )
    return clerk, timer, passes


async def test_a_closing_clerks_last_round_takes_no_close_and_the_next_start_takes_it(paper: PaperOptions) -> None:  # noqa: F811
    """The close came due and no round has run since. The Clerk closes. Its last round sends no new order."""
    await _holds_one_call(paper)
    _until(paper, DUE)

    await _the_clerk(paper).close()
    _open_the_books_again(paper)

    assert _closes_sent(paper) == [] and paper.repo.active_exit_for_strategy("a") is None
    [left] = scheduled_close_state(paper.repo, "a")
    assert (left.standing, left.taken_at_ms) == (CloseStanding.SCHEDULED, None)

    paper.wait(PASS_MS)
    await paper.round()

    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE]


async def test_a_close_taken_and_cut_by_the_stop_before_its_order_was_sent_is_sent_by_the_next_start(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    await _holds_one_call(paper)
    _until(paper, DUE)
    never = asyncio.Event()
    with monkeypatch.context() as held_before_the_send:
        _driving_of("a", never.wait, held_before_the_send)
        paper.keeper.start()
        assert await _comes_true(lambda: paper.repo.active_exit_for_strategy("a") is not None)

        await asyncio.wait_for(_the_clerk(paper).close(), timeout=5)

    # The close was accepted and nothing was sent. It stays on the books as taken.
    _open_the_books_again(paper)
    assert _closes_sent(paper) == [] and paper.repo.active_exit_for_strategy("a") is not None

    await _the_next_start(paper)

    # The next start sends it once, as it finishes every exit it finds unfinished.
    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE] and len(_closing_refs(paper)) == 1
    await _the_close_fills(paper)
    assert paper.held() == {}


@pytest.mark.parametrize("the_order_landed", [True, False], ids=["the venue took it", "the venue never got it"])
async def test_a_close_cut_by_the_stop_inside_its_send_is_never_sent_a_second_time_by_the_next_start(
    paper: PaperOptions, the_order_landed: bool  # noqa: F811
) -> None:
    """The stop lands while Alpaca has not answered the send. Whether the order landed is not known to the Clerk."""
    await _holds_one_call(paper)
    _until(paper, DUE)
    being_sent = asyncio.Event()
    real_send = paper.venue.submit_option_order

    async def send_that_alpaca_never_answers(order: object, *, client_order_id: str) -> object:
        if the_order_landed:
            await real_send(order, client_order_id=client_order_id)  # type: ignore[arg-type]
        being_sent.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    paper.venue.submit_option_order = send_that_alpaca_never_answers  # type: ignore[method-assign]
    paper.keeper.start()
    await asyncio.wait_for(being_sent.wait(), timeout=2)

    await asyncio.wait_for(_the_clerk(paper).close(), timeout=5)
    paper.venue.submit_option_order = real_send  # type: ignore[method-assign]
    at_the_venue_then = [payload["side"] for payload in paper.venue.option_orders.values()]
    asked_to_send_then = len(paper.venue.option_submit_calls)

    _open_the_books_again(paper)
    await _the_next_start(paper)
    for _ in range(8):  # two minutes more of passes, with a round each
        paper.wait(PASS_MS)
        await paper.pass_()
        await paper.round()

    # An order that landed is found by its own id and nothing more is sent. One that never landed is sent once.
    assert at_the_venue_then == (["buy", "sell"] if the_order_landed else ["buy"])
    assert len(paper.venue.option_submit_calls) - asked_to_send_then == (0 if the_order_landed else 1)
    assert [payload["side"] for payload in paper.venue.option_orders.values()] == ["buy", "sell"]
    await _the_close_fills(paper)
    assert paper.held() == {} and paper.repo.active_exit_for_strategy("a") is None


# ── a close that is due and not yet taken when the Clerk begins to close ──


async def test_a_pass_that_begins_while_the_clerk_is_closing_takes_no_close_and_the_next_start_takes_it(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The close came due. The Clerk closes, and its last round waits for Alpaca to answer a cancel. The
    reconciliation loop's timer fires in that time, and a whole pass runs before the loop is stopped."""
    await _holds_one_call(paper, "a")
    # The account is read again after a's fill, as its own cadence reads it, before b enters.
    paper.wait(1_000)
    paper.account.account_observed_at_ms = paper.clock()
    assert await paper.sync.tick() == "observed"
    await paper.resting("b")
    clerk, timer, passes = _the_clerk_with_its_reconciliation_loop(paper)
    # The staging below is the test's own, so the last round's few seconds are not what ends it.
    monkeypatch.setattr(keeper_module, "LAST_ROUND_LIMIT_MS", 60_000)
    cancel_asked, alpaca_answers = asyncio.Event(), asyncio.Event()
    real_cancel = paper.venue.cancel

    async def cancel_that_alpaca_is_slow_to_answer(order_id: str) -> None:
        cancel_asked.set()
        await alpaca_answers.wait()
        await real_cancel(order_id)

    loop = clerk.sweep
    assert loop is not None
    loop.start()
    try:
        # The loop's first pass runs at noon, with nothing due. Then nothing looks until the close is due.
        first_pass_ran = await _comes_true(lambda: len(passes) == 1)
        _until(paper, DUE)
        paper.venue.cancel = cancel_that_alpaca_is_slow_to_answer  # type: ignore[method-assign]

        closing = asyncio.create_task(clerk.close())
        # Noted now and judged below, so that a failure never leaves the cancel unanswered.
        the_last_round_waited = await _comes_true(cancel_asked.is_set)
        timer.fires()
        a_pass_ran = await _comes_true(lambda: len(passes) == 2)
        still_closing = not closing.done()
        alpaca_answers.set()
        await asyncio.wait_for(closing, timeout=5)
    finally:
        await loop.stop()

    # A whole pass ran inside the shutdown. It took no close: nothing was accepted and nothing was sent.
    assert first_pass_ran and the_last_round_waited and a_pass_ran and still_closing
    _open_the_books_again(paper)
    assert _closes_sent(paper) == [] and paper.repo.active_exit_for_strategy("a") is None
    [left] = scheduled_close_state(paper.repo, "a")
    assert (left.standing, left.taken_at_ms) == (CloseStanding.SCHEDULED, None)

    await _the_next_start(paper)

    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE] and len(_closing_refs(paper, "a")) == 1


async def test_the_keepers_own_round_while_the_clerk_finishes_what_it_was_sending_takes_no_close(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Bot b's own exit is still being sent when the Clerk begins to close, and the Clerk waits for it. The
    keeper keeps its deadlines until its last round, and wakes in that time with bot a's close due."""
    await _two_bots_each_hold_one_call(paper)
    timer = _Timer()
    paper.keeper = paper.another_keeper(rest=timer.sleep)
    paper.keeper.start()
    assert await _comes_true(lambda: timer.sleeps == 1)
    _until(paper, DUE)
    being_sent, alpaca_answers = asyncio.Event(), asyncio.Event()
    real_send = paper.venue.submit_option_order

    async def send_whose_first_answer_is_slow(order: object, *, client_order_id: str) -> object:
        if not being_sent.is_set():
            being_sent.set()
            await alpaca_answers.wait()
        return await real_send(order, client_order_id=client_order_id)  # type: ignore[arg-type]

    paper.venue.submit_option_order = send_whose_first_answer_is_slow  # type: ignore[method-assign]
    bs_own_exit = asyncio.create_task(paper.exit("b"))
    await asyncio.wait_for(being_sent.wait(), timeout=2)

    closing = asyncio.create_task(_the_clerk(paper).close())
    for _ in range(20):
        await asyncio.sleep(0)
    timer.fires()
    # Noted now and judged below, so that a failure never leaves the send unanswered.
    the_round_ran = await _comes_true(lambda: timer.sleeps == 2)
    still_closing = not closing.done()
    alpaca_answers.set()
    await asyncio.wait_for(closing, timeout=5)
    await asyncio.wait_for(bs_own_exit, timeout=5)

    # The keeper's round ran to its end while the Clerk was still closing, and took no close of bot a's.
    assert the_round_ran and still_closing
    _open_the_books_again(paper)
    assert _closing_refs(paper, "a") == [] and paper.repo.active_exit_for_strategy("a") is None
    # The one closing order at the venue is bot b's own, which the Clerk had already accepted.
    assert _closes_sent(paper) == [SELLS_ONE_TO_CLOSE] and len(_closing_refs(paper, "b")) == 1

    await _the_next_start(paper)

    assert len(_closing_refs(paper, "a")) == 1 and _closes_sent(paper) == [SELLS_ONE_TO_CLOSE, SELLS_ONE_TO_CLOSE]


async def test_a_failed_close_is_not_sent_again_once_the_clerk_has_begun_to_close_and_the_next_start_sends_it(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Alpaca refused the close and takes orders again. Its next try is ready when the Clerk begins to close."""
    await _holds_one_call(paper)
    _until(paper, DUE)
    paper.venue.option_submit_error = BrokerOrderRejected("the venue refuses option orders", broker="alpaca")
    await paper.round()
    paper.venue.option_submit_error = None
    paper.wait(PASS_MS)
    await paper.pass_()
    paper.wait(SCHEDULED_CLOSE_RETRY_AFTER_MS - PASS_MS)
    asked = len(paper.venue.option_submit_calls)

    await paper.clerk.begin_closing()
    await paper.pass_()
    await paper.round()

    # The pass that would have sent it again, and the keeper's round, send nothing more.
    assert len(paper.venue.option_submit_calls) == asked and paper.repo.active_exit_for_strategy("a") is None

    await _the_clerk(paper).close()
    _open_the_books_again(paper)
    await _the_next_start(paper)

    assert len(paper.venue.option_submit_calls) == asked + 1
    await _the_close_fills(paper)
    assert paper.held() == {}
