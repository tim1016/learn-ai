"""What a Clerk that closes does with the option entries still resting at Alpaca (#2913).

The real Clerk of a Paper account, its real deadline keeper and its real
books, held by the runtime ``main.py`` closes. The venue is the Paper
harness's own. What is asserted is what the owner would find afterwards:
whether the order is still in the market, and what the books say of it once
a new Clerk opens them.

The last test is about the keeper's ordinary step: which entries are asked
about first when more are due than are asked about at once.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca import client as alpaca_client
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite import deadline_keeper
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.deadline_keeper import DeadlineKeeper, Nudge
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.broker.alpaca.clerk.sqlite.off_loop import OffLoop, run_inline, to_thread
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, ExecutionLeaseLost
from app.broker.alpaca.clerk.sqlite.resting_entries import (
    MAX_ENTRIES_AT_ONCE,
    WaitEndedRound,
    cancel_every_resting_entry,
    cancel_resting_entries,
    entries_past_their_wait,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.errors import BrokerUnavailable
from tests.broker.alpaca.clerk.sqlite.conftest import (
    FIXTURE_RTH_MS,
    _broker_leg,
    _FakeReadPort,
    _FakeTradePort,
    _TestClock,
)
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    WAIT_MS,
    PaperOptions,
    _compose,
    paper,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS, _gate

# The harness's keeper works on the test's own thread. Production's works on worker threads.
EITHER_WAY = pytest.mark.parametrize("off_loop", [run_inline, to_thread], ids=["on the loop", "on worker threads"])


def _the_clerk(
    paper: PaperOptions, *, authority_kind: str = "sqlite", off_loop: OffLoop = run_inline  # noqa: F811
) -> ActiveClerkRuntime:
    """The account's Clerk as ``main.py`` holds it: its facade, its account reading, its keeper, its books."""
    if off_loop is not run_inline:
        paper.keeper = paper.another_keeper(off_loop=off_loop)
    return ActiveClerkRuntime(
        authority_kind=authority_kind,  # type: ignore[arg-type]
        clerk=paper.clerk, envelope_sync=paper.sync, deadline_keeper=paper.keeper,
        _sqlite_repository=paper.repo, account_id=paper.repo.account_id,
    )


def _open_the_books_again(paper: PaperOptions) -> None:  # noqa: F811
    """A new Clerk over the same books, before any startup recovery: what the closed one left behind."""
    paper.repo = ClerkSqliteRepository.open(
        account_id=paper.repo.account_id, artifacts_root=paper.artifacts_root, clock=paper.clock
    )
    paper.sync, paper.keeper, paper.clerk = _compose(paper.repo, paper.clock, paper.venue, paper.account)


def _asks(venue: _FakeTradePort) -> tuple[list[str], ...]:
    """Everything the venue was asked so far, by kind."""
    return (
        list(venue.submit_calls), list(venue.option_submit_calls), list(venue.cancel_calls),
        list(venue.lookup_calls), list(venue.option_lookup_calls),
    )


# ── the close cancels what rests ─────────────────────────────────────────


async def test_an_entry_still_being_sent_when_the_clerk_closes_is_cancelled_and_proven_before_the_books_close(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    seen: list[str] = []
    being_sent, alpaca_answers = asyncio.Event(), asyncio.Event()
    venue = paper.venue
    real_send, real_cancel, real_lookup = venue.submit_option_order, venue.cancel, venue.get_option_order_by_client_order_id
    real_close = paper.repo.close

    async def send_that_alpaca_is_slow_to_answer(order: object, *, client_order_id: str) -> object:
        being_sent.set()
        await alpaca_answers.wait()
        sent = await real_send(order, client_order_id=client_order_id)  # type: ignore[arg-type]
        seen.append("the entry rests")
        return sent

    async def cancel(order_id: str) -> None:
        seen.append("cancel")
        await real_cancel(order_id)

    async def lookup(client_order_id: str) -> object:
        seen.append("lookup")
        return await real_lookup(client_order_id)

    def close_the_books() -> None:
        seen.append("the books close")
        real_close()

    venue.submit_option_order = send_that_alpaca_is_slow_to_answer  # type: ignore[method-assign]
    venue.cancel = cancel  # type: ignore[method-assign]
    venue.get_option_order_by_client_order_id = lookup  # type: ignore[method-assign]
    monkeypatch.setattr(paper.repo, "close", close_the_books)
    clerk = _the_clerk(paper)

    # Shutdown stops the bot while its entry is being sent. The entry was accepted, and goes on.
    bot = asyncio.create_task(paper.enter())
    await asyncio.wait_for(being_sent.wait(), timeout=2)
    bot.cancel()
    with suppress(asyncio.CancelledError):
        await bot
    closing = asyncio.create_task(clerk.close())
    for _ in range(20):
        await asyncio.sleep(0)
    # Noted now and judged below, so that a failure never leaves the send unanswered.
    while_the_send_waited = (list(seen), closing.done())

    alpaca_answers.set()
    await asyncio.wait_for(closing, timeout=5)
    _open_the_books_again(paper)

    # The close waited for the send: nothing was asked of Alpaca meanwhile, and the books stayed open.
    assert while_the_send_waited == ([], False)
    # Then in this order, and nothing after the books closed.
    assert seen == ["the entry rests", "cancel", "lookup", "the books close"]
    [order_ref] = venue.option_orders
    assert venue.option_orders[order_ref]["status"] == "canceled"
    assert [why for why, _at in paper.cancel_requests(order_ref)] == ["ENTRY_CLERK_CLOSED"]
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED
    assert paper.held() == {} and paper.set_aside_cents() == 0


async def test_while_a_closing_clerk_waits_for_an_entry_being_sent_another_bots_deadline_is_still_kept(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """The keeper is not stopped until the accepted effects are finished: a deadline that falls meanwhile is kept."""
    resting = await paper.resting("b")
    being_sent, alpaca_answers = asyncio.Event(), asyncio.Event()
    real_send = paper.venue.submit_option_order

    async def send_that_alpaca_is_slow_to_answer(order: object, *, client_order_id: str) -> object:
        being_sent.set()
        await alpaca_answers.wait()
        return await real_send(order, client_order_id=client_order_id)  # type: ignore[arg-type]

    paper.venue.submit_option_order = send_that_alpaca_is_slow_to_answer  # type: ignore[method-assign]
    paper.keeper.start()
    bot = asyncio.create_task(paper.enter("a"))
    await asyncio.wait_for(being_sent.wait(), timeout=2)
    bot.cancel()
    with suppress(asyncio.CancelledError):
        await bot
    closing = asyncio.create_task(_the_clerk(paper).close())
    for _ in range(20):
        await asyncio.sleep(0)

    # The other bot's wait runs out while the close still waits for the send. The nudge spares the test a real sleep.
    paper.wait(WAIT_MS)
    paper.keeper.nudge()
    for _ in range(50):
        await asyncio.sleep(0)
    # Noted now and judged below, so that a failure never leaves the send unanswered.
    while_the_send_waited = (closing.done(), paper.cancel_requests(resting), paper.outcome(resting).standing)
    waited_until = paper.clock()

    alpaca_answers.set()
    await asyncio.wait_for(closing, timeout=5)
    _open_the_books_again(paper)

    assert while_the_send_waited == (False, [("ENTRY_WAIT_ENDED", waited_until)], EntryStanding.UNFILLED)
    # And the entry that was being sent did not outlive the close either.
    assert {payload["status"] for payload in paper.venue.option_orders.values()} == {"canceled"}


@EITHER_WAY
async def test_an_entry_resting_well_inside_its_wait_is_cancelled_and_proven_when_the_clerk_closes(
    paper: PaperOptions, off_loop: OffLoop  # noqa: F811
) -> None:
    order_ref = await paper.resting()
    paper.wait(1_000)
    closed_at = paper.clock()
    assert paper.outcome(order_ref).cancel_at_ms - closed_at == WAIT_MS - 1_000

    await _the_clerk(paper, off_loop=off_loop).close()
    _open_the_books_again(paper)

    assert paper.venue.option_orders[order_ref]["status"] == "canceled"
    assert paper.cancel_requests(order_ref) == [("ENTRY_CLERK_CLOSED", closed_at)]
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED
    assert paper.held() == {} and paper.set_aside_cents() == 0


async def test_an_entry_that_filled_in_part_keeps_what_filled_when_the_clerk_closes(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting(units=2)
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")

    await _the_clerk(paper).close()
    _open_the_books_again(paper)

    assert paper.outcome(order_ref).standing is EntryStanding.PART_FILLED
    assert sum(paper.held().values()) == 1.0


async def test_an_entry_the_keepers_own_round_was_cancelling_when_the_clerk_closes_is_still_cancelled_and_proven(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """The keeper's task is ended first. Left running, its round would hold the entry's claim against the last one."""
    order_ref = await paper.resting()
    paper.wait(WAIT_MS)
    inside_the_cancel = asyncio.Event()
    real_cancel = paper.venue.cancel
    asks = 0

    async def cancel_whose_first_ask_is_never_answered(order_id: str) -> None:
        nonlocal asks
        asks += 1
        if asks == 1:
            inside_the_cancel.set()
            await asyncio.Event().wait()
        await real_cancel(order_id)

    paper.venue.cancel = cancel_whose_first_ask_is_never_answered  # type: ignore[method-assign]
    paper.keeper.start()
    await asyncio.wait_for(inside_the_cancel.wait(), timeout=2)

    await asyncio.wait_for(_the_clerk(paper).close(), timeout=10)
    _open_the_books_again(paper)

    assert asks == 2 and paper.venue.option_orders[order_ref]["status"] == "canceled"
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED


async def test_a_clerk_closed_twice_does_nothing_the_second_time(
    paper: PaperOptions, caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    order_ref = await paper.resting()
    clerk = _the_clerk(paper)
    await clerk.close()
    asked = _asks(paper.venue)

    with caplog.at_level(logging.WARNING):
        await clerk.close()
    _open_the_books_again(paper)

    assert _asks(paper.venue) == asked
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    assert len(paper.recorded(order_ref, "ORDER_CANCEL_REQUESTED")) == 1


# ── a broker that does not answer ────────────────────────────────────────


@EITHER_WAY
async def test_a_broker_that_never_answers_the_cancel_holds_the_close_no_longer_than_its_limit(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, off_loop: OffLoop  # noqa: F811
) -> None:
    order_ref = await paper.resting()
    real_cancel = paper.venue.cancel

    async def cancel_that_never_answers(_order_id: str) -> None:
        await asyncio.Event().wait()

    paper.venue.cancel = cancel_that_never_answers  # type: ignore[method-assign]
    monkeypatch.setattr(deadline_keeper, "LAST_ROUND_LIMIT_MS", 200)
    closing = asyncio.create_task(_the_clerk(paper, off_loop=off_loop).close())

    # Watched from outside: a wait_for would itself end a close that had not returned.
    with caplog.at_level(logging.WARNING, logger=deadline_keeper.__name__):
        returned, _still_closing = await asyncio.wait({closing}, timeout=10)
    closing.cancel()
    [outcome] = await asyncio.gather(closing, return_exceptions=True)
    assert returned == {closing} and outcome is None
    _open_the_books_again(paper)

    # A broker that is silent is told as that, once, and not as a failure of the Clerk.
    assert [record.levelno for record in caplog.records if record.name == deadline_keeper.__name__] == [logging.WARNING]

    # Left as any cancel stopped in the middle is: asked for on the books, its outcome unknown, still in the market.
    assert [why for why, _at in paper.cancel_requests(order_ref)] == ["ENTRY_CLERK_CLOSED"]
    assert len(paper.recorded(order_ref, "ORDER_CANCEL_UNCERTAIN")) == 1
    assert paper.outcome(order_ref).standing is EntryStanding.OUTCOME_UNKNOWN
    assert paper.venue.option_orders[order_ref]["status"] == "new"

    # The next start finishes it.
    paper.venue.cancel = real_cancel  # type: ignore[method-assign]
    await paper.clerk.recover()

    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED
    assert paper.venue.option_orders[order_ref]["status"] == "canceled" and paper.set_aside_cents() == 0


async def test_a_cancel_alpaca_took_and_does_not_show_yet_is_left_asked_for_and_the_next_start_proves_it(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """An order Alpaca's id is known for is asked about once. A lookup that still shows it working proves nothing."""
    order_ref = await paper.resting()
    paper.venue.cancel_is_slow = True

    await _the_clerk(paper).close()
    _open_the_books_again(paper)

    assert len(paper.venue.cancel_calls) == 1
    assert paper.outcome(order_ref).standing is EntryStanding.CANCELLATION_REQUESTED

    paper.venue.cancel_is_slow = False
    await paper.clerk.recover()

    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED and paper.set_aside_cents() == 0


def test_the_last_rounds_limit_is_shorter_than_one_call_to_alpaca_may_take() -> None:
    """Were it longer, the Alpaca client's own timeout would bound the close, and the limit would bound nothing."""
    assert deadline_keeper.LAST_ROUND_LIMIT_MS < alpaca_client._DEFAULT_TIMEOUT_S * 1000


async def test_a_last_round_that_fails_is_logged_and_the_books_are_still_closed(
    paper: PaperOptions, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    """A lost lease comes out of the step as an error. The closing Clerk must still close its books."""
    order_ref = await paper.resting()
    real_cancel, real_renewal = paper.venue.cancel, paper.repo.renew_operation_claim
    cancel_answered = False

    async def cancel(order_id: str) -> None:
        nonlocal cancel_answered
        await real_cancel(order_id)
        cancel_answered = True

    def renewal_that_finds_the_lease_gone_after_the_cancel(**claim: object) -> bool:
        nonlocal cancel_answered
        if cancel_answered:
            cancel_answered = False
            raise ExecutionLeaseLost("another Clerk now holds this account", account_id=paper.repo.account_id)
        return real_renewal(**claim)

    paper.venue.cancel = cancel  # type: ignore[method-assign]
    monkeypatch.setattr(paper.repo, "renew_operation_claim", renewal_that_finds_the_lease_gone_after_the_cancel)

    with caplog.at_level(logging.ERROR, logger=deadline_keeper.__name__):
        await _the_clerk(paper).close()
    # Opening the books again is itself the proof they were closed: a handle left open still holds the account.
    _open_the_books_again(paper)

    assert [record.levelno for record in caplog.records if record.name == deadline_keeper.__name__] == [logging.ERROR]
    # The cancel was asked for and taken. Its proof was not read, and is the next start's.
    assert paper.venue.option_orders[order_ref]["status"] == "canceled"
    assert paper.outcome(order_ref).standing is EntryStanding.CANCELLATION_REQUESTED

    await paper.clerk.recover()

    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED


async def test_an_entry_whose_answer_was_lost_is_found_and_cancelled_by_the_close(paper: PaperOptions) -> None:  # noqa: F811
    """With no broker id there is nothing to cancel by, and no round follows the last one. So it is asked twice."""
    paper.venue.option_answer_lost = True
    paper.venue.option_lookup_error = BrokerUnavailable("lookup timed out", broker="alpaca")
    sent = await paper.enter()
    [order_ref] = sent.child_order_refs
    landed = paper.repo.order(order_ref)
    # The order is at Alpaca, working, and the books do not know Alpaca's id for it.
    assert landed is not None and landed.broker_order_id is None
    assert paper.venue.option_orders[order_ref]["status"] == "new"
    paper.venue.option_answer_lost, paper.venue.option_lookup_error = False, None

    await _the_clerk(paper).close()
    _open_the_books_again(paper)

    assert paper.venue.cancel_calls == [paper.venue.option_orders[order_ref]["id"]]
    assert paper.venue.option_orders[order_ref]["status"] == "canceled"
    assert [why for why, _at in paper.cancel_requests(order_ref)] == ["ENTRY_CLERK_CLOSED"]
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED and paper.set_aside_cents() == 0


async def test_the_closing_step_says_which_entries_it_proved_and_which_it_left(paper: PaperOptions) -> None:  # noqa: F811
    """One entry found by its first lookup and cancelled by the second ask, beside one whose cancel is not shown yet."""
    paper.venue.option_answer_lost = True
    paper.venue.option_lookup_error = BrokerUnavailable("lookup timed out", broker="alpaca")
    [lost_answer] = (await paper.enter("a")).child_order_refs
    paper.venue.option_answer_lost, paper.venue.option_lookup_error = False, None
    paper.wait(1_000)
    shown_working = await paper.resting("b")
    real_cancel = paper.venue.cancel

    async def cancel_that_alpaca_does_not_show_for_one(order_id: str) -> None:
        paper.venue.cancel_is_slow = order_id == paper.venue.option_orders[shown_working]["id"]
        await real_cancel(order_id)

    paper.venue.cancel = cancel_that_alpaca_does_not_show_for_one  # type: ignore[method-assign]

    done = await cancel_every_resting_entry(paper.repo, trade=paper.venue)

    assert done == WaitEndedRound(ended=(lost_answer,), still_due=(shown_working,))
    assert paper.outcome(lost_answer).standing is EntryStanding.UNFILLED
    assert paper.outcome(shown_working).standing is EntryStanding.CANCELLATION_REQUESTED


# ── who makes no last round, and what it never touches ───────────────────


async def test_a_simulations_clerk_cancels_nothing_when_it_closes(paper: PaperOptions) -> None:  # noqa: F811
    """A simulation rests nothing. Held over books that do rest an entry, its close still asks for no cancel."""
    order_ref = await paper.resting()
    asked = _asks(paper.venue)

    await _the_clerk(paper, authority_kind="synthetic").close()
    _open_the_books_again(paper)

    assert _asks(paper.venue) == asked and paper.cancel_requests(order_ref) == []
    assert paper.outcome(order_ref).standing is EntryStanding.WORKING


async def test_a_stock_only_accounts_clerk_asks_alpaca_nothing_and_writes_nothing_when_it_closes(tmp_path: Path) -> None:
    def log(books: ClerkSqliteRepository) -> list[tuple[object, ...]]:
        with books.write_fence() as conn:
            return [tuple(row) for row in conn.execute("SELECT * FROM custody_transitions ORDER BY sequence").fetchall()]

    clock = _TestClock(FIXTURE_RTH_MS)
    repo = ClerkSqliteRepository.initialize(account_id="PA-STOCK", artifacts_root=tmp_path, clock=clock)
    repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
    submit_start_run(repo, account_id="PA-STOCK", strategy_instance_id="spy-bot", lifecycle_run_id="run-1")
    venue = _FakeTradePort()
    sent = await submit_enter(
        repo, account_id="PA-STOCK", strategy_instance_id="spy-bot", decision_id="d-1", lifecycle_run_id="run-1",
        leg=_broker_leg(), trade=venue,
    )
    assert sent.order_ref is not None
    working = repo.order(sent.order_ref)
    # The scenario is not an empty one: a stock entry is working at the broker, under a run still going.
    assert working is not None and working.broker_state == "accepted" and repo.active_run("spy-bot") is not None
    written, asked = log(repo), _asks(venue)
    nudge = Nudge()
    facade = SqliteAlpacaClerkFacade(
        repo=repo, read=_FakeReadPort(), trade=venue, account_mode="paper", entry_deadline_nudge=nudge
    )
    keeper = DeadlineKeeper(
        repo=repo, trade=venue, intake=facade.intake, pricing=facade.recovery_pricing, nudge=nudge, off_loop=run_inline
    )
    clerk = ActiveClerkRuntime(
        authority_kind="sqlite", clerk=facade, deadline_keeper=keeper, _sqlite_repository=repo, account_id="PA-STOCK",
    )

    await clerk.close()

    reopened = ClerkSqliteRepository.open(account_id="PA-STOCK", artifacts_root=tmp_path, clock=clock)
    try:
        assert log(reopened) == written
        assert reopened.order(sent.order_ref) == working
    finally:
        reopened.close()
    assert _asks(venue) == asked


# ── more due at once than are asked about at once ────────────────────────


def _one_more_bot(paper: PaperOptions, sid: str) -> None:  # noqa: F811
    """One more bot of $500.00 on the account, deployed against a reading roomy enough for its budget."""
    paper.repo.register_strategy_instance(
        strategy_instance_id=sid, symbol="SPY", config_hash=f"seal-{sid}", exit_terms=TERMS
    )
    submit_budgeted_deploy(
        paper.repo, strategy_instance_id=sid, lifecycle_run_id=f"run-{sid}", world="real_paper",
        committed_cents=50_000, configuration_hash=f"seal-{sid}",
        exit_terms_hash=canonical_sha256(TERMS.model_dump(mode="json")), risk_revision=1, actor="owner",
        envelope=_gate(cash=10_000), minimum_position_cost=Decimal("100.01"),
    )


async def test_with_five_entries_due_at_once_the_four_oldest_are_asked_about_first_and_the_newest_waits(
    paper: PaperOptions,  # noqa: F811
) -> None:
    bots = ("a", "b", "c", "d", "e")
    for sid in bots[2:]:
        _one_more_bot(paper, sid)
    order_refs: list[str] = []
    for sid in bots:
        order_refs.append(await paper.resting(sid))
        paper.wait(1_000)
    paper.wait(WAIT_MS)
    due = entries_past_their_wait(paper.repo, paper.clock())
    # All five are due, each a second after the one before.
    assert [entry.order.order_ref for entry in due] == order_refs
    assert len({entry.cancel_at_ms for entry in due}) == len(bots) == MAX_ENTRIES_AT_ONCE + 1
    alpaca_answers = asyncio.Event()
    asked: list[str] = []
    real_cancel = paper.venue.cancel

    async def cancel_that_waits_for_alpaca(order_id: str) -> None:
        asked.append(order_id)
        await alpaca_answers.wait()
        await real_cancel(order_id)

    paper.venue.cancel = cancel_that_waits_for_alpaca  # type: ignore[method-assign]
    alpacas_ids = [paper.venue.option_orders[order_ref]["id"] for order_ref in order_refs]

    # Handed over newest first: who goes first is the deadlines' to say, not the list's.
    round_ = asyncio.create_task(cancel_resting_entries(paper.repo, list(reversed(due)), trade=paper.venue))
    for _ in range(50):
        await asyncio.sleep(0)
    # Noted now and judged below, so that a failure never leaves a cancel unanswered.
    while_alpaca_was_silent = (list(asked), paper.cancel_requests(order_refs[-1]))

    alpaca_answers.set()
    done = await asyncio.wait_for(round_, timeout=5)

    # The four oldest were at Alpaca, the oldest asked first. The newest waited for a place, untouched.
    assert while_alpaca_was_silent == (alpacas_ids[:MAX_ENTRIES_AT_ONCE], [])
    assert asked == alpacas_ids
    assert done.ended == tuple(order_refs) and done.still_due == ()
