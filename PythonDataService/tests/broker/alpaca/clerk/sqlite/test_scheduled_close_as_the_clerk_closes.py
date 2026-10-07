"""What a Clerk that closes does with a scheduled close, and what the next start finds (#2913).

A closing Clerk sends no new order: its keeper's last round cancels resting
entries and takes no close. But the keeper keeps its deadlines until that
last round, so a close can be taken while the Clerk is closing and then be
cut by the stop, anywhere. These tests cut it in each place and assert what
the owner would find afterwards: the close is not lost, and the venue was
never sent a second closing order.

The Clerk is the Paper harness's, held by the runtime ``main.py`` closes.
Nothing here was sent to Alpaca or recorded from it.
"""

from __future__ import annotations

import asyncio

import pytest

from app.broker.alpaca.clerk.sqlite.scheduled_close import CloseStanding, scheduled_close_state
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
    _until,
)


async def _the_next_start(paper: PaperOptions) -> None:  # noqa: F811
    """The new Clerk over the books the closed one left runs its startup recovery and makes its first pass."""
    await paper.clerk.recover()
    paper.wait(PASS_MS)
    await paper.pass_()


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
