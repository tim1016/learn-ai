"""The Clerk's own task for a resting entry's deadline: when it acts, and what happens without it (#2913).

Time here is a clock the test moves. The keeper's rest between rounds is
replaced by one that moves that clock, so "within two seconds of the
deadline" is asserted on the times the Clerk itself recorded, with no
sleeping. Its real timing under the production event loop is not shown here.
"""

from __future__ import annotations

import asyncio

from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.sqlite import deadline_keeper
from app.broker.alpaca.clerk.sqlite.deadline_keeper import (
    IDLE_WAKE_MS,
    UNPROVEN_RETRY_MS,
    UNPROVEN_RETRY_WINDOW_MS,
    DeadlineKeeper,
    rest_until_nudged,
)
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.broker.alpaca.clerk.sqlite.off_loop import run_inline
from app.broker.contract.errors import BrokerUnavailable
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    WAIT_MS,
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import (
    _DryRun,
    dry_run,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.test_active_runtime_taps import _FakeTap


def _keeper_on_the_tests_clock(paper: PaperOptions, *, rounds: int) -> tuple[DeadlineKeeper, list[int]]:  # noqa: F811
    """A keeper whose rest moves the test's clock by what it asked to sleep; and each sleep it asked for, in ms."""
    slept: list[int] = []

    async def rest(seconds: float, _nudged: asyncio.Event) -> None:
        slept.append(round(seconds * 1000))
        paper.wait(round(seconds * 1000))

    return DeadlineKeeper(repo=paper.repo, trade=paper.venue, rest=rest, off_loop=run_inline, max_rounds=rounds), slept


# ── on time ───────────────────────────────────────────────────────────────


async def test_the_cancel_is_asked_for_within_two_seconds_of_the_deadline(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    deadline = paper.outcome(order_ref).cancel_at_ms
    paper.wait(3_700)  # so the deadline falls on no round boundary of the keeper's own
    keeper, slept = _keeper_on_the_tests_clock(paper, rounds=5)

    await keeper.run()

    [(why, asked_at)] = paper.cancel_requests(order_ref)
    assert why == "ENTRY_WAIT_ENDED"
    assert 0 <= asked_at - deadline <= 2_000
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED
    # It never slept past the deadline: three idle wakes, then exactly the time left to it.
    assert slept[:4] == [IDLE_WAKE_MS, IDLE_WAKE_MS, IDLE_WAKE_MS, 1_300]
    assert 3_700 + sum(slept[:4]) == WAIT_MS


async def test_with_nothing_resting_the_keeper_only_wakes_idly_and_writes_nothing(paper: PaperOptions) -> None:  # noqa: F811
    before = paper.repo.last_custody_sequence()

    done = await paper.round()

    assert done.wake_in_ms == IDLE_WAKE_MS
    assert paper.repo.last_custody_sequence() == before


async def test_the_keeper_sleeps_to_the_next_deadline_and_no_further(paper: PaperOptions) -> None:  # noqa: F811
    await paper.resting()
    paper.wait(WAIT_MS - 1_300)

    done = await paper.round()

    assert done.wake_in_ms == 1_300


async def test_an_unproven_cancel_is_looked_at_again_every_second_for_ten_seconds_then_idly(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    paper.venue.cancel_error = BrokerUnavailable("Alpaca did not answer", broker="alpaca")
    paper.wait(WAIT_MS)

    assert (await paper.round()).wake_in_ms == UNPROVEN_RETRY_MS

    paper.wait(UNPROVEN_RETRY_WINDOW_MS - 1)
    assert (await paper.round()).wake_in_ms == UNPROVEN_RETRY_MS
    paper.wait(1)
    assert (await paper.round()).wake_in_ms == IDLE_WAKE_MS
    assert paper.outcome(order_ref).standing is EntryStanding.OUTCOME_UNKNOWN


async def test_two_bots_on_one_account_one_at_its_deadline_while_the_other_enters_and_is_not_refused(
    paper: PaperOptions,  # noqa: F811
) -> None:
    first = await paper.resting("a")
    paper.wait(WAIT_MS)

    # In the same second: A's cancel and its proof, and B's entry.
    cancelled, entered = await asyncio.gather(paper.round(), paper.enter("b"))

    assert cancelled.entries.ended == (first,)
    assert entered.state is EffectOperationState.SUBMITTED, entered.explanation
    [second] = entered.child_order_refs
    assert paper.outcome(first).standing is EntryStanding.UNFILLED
    assert paper.outcome(second).standing is EntryStanding.WORKING
    # B's own deadline is its own: twenty seconds from its admission, not from A's.
    assert paper.outcome(second).cancel_at_ms == paper.clock() + WAIT_MS


# ── the task itself ──────────────────────────────────────────────────────


async def test_accepting_an_entry_wakes_a_sleeping_keeper(paper: PaperOptions) -> None:  # noqa: F811
    woken: list[bool] = []

    async def rest(_seconds: float, nudged: asyncio.Event) -> None:
        await asyncio.wait_for(nudged.wait(), timeout=5)
        woken.append(nudged.is_set())

    keeper = DeadlineKeeper(repo=paper.repo, trade=paper.venue, rest=rest, off_loop=run_inline, max_rounds=1)
    paper.clerk._entry_deadline_nudge = keeper.nudge
    running = asyncio.create_task(keeper.run())
    await asyncio.sleep(0)

    await paper.resting()
    await running

    assert woken == [True]


async def test_a_nudge_is_spent_by_the_round_that_follows_it(paper: PaperOptions) -> None:  # noqa: F811
    """A nudge that was never cleared would end every rest at once, and the keeper would spin."""
    seen: list[bool] = []

    async def rest(_seconds: float, nudged: asyncio.Event) -> None:
        seen.append(nudged.is_set())

    keeper = DeadlineKeeper(repo=paper.repo, trade=paper.venue, rest=rest, off_loop=run_inline, max_rounds=2)
    keeper.nudge()

    await keeper.run()

    assert seen == [False, False]


async def test_the_keepers_rest_ends_at_its_time_or_at_a_nudge_whichever_is_first() -> None:
    quiet, nudged = asyncio.Event(), asyncio.Event()
    nudged.set()

    await asyncio.wait_for(rest_until_nudged(0.01, quiet), timeout=2)
    await asyncio.wait_for(rest_until_nudged(30, nudged), timeout=2)


async def test_a_round_that_fails_does_not_end_the_keeper(paper: PaperOptions, monkeypatch) -> None:  # noqa: F811
    order_ref = await paper.resting()
    paper.wait(WAIT_MS)
    real_step = deadline_keeper.cancel_entries_past_their_wait
    calls = 0

    async def step_that_fails_once(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("the books could not be read")
        return await real_step(*args, **kwargs)

    monkeypatch.setattr(deadline_keeper, "cancel_entries_past_their_wait", step_that_fails_once)
    keeper, _slept = _keeper_on_the_tests_clock(paper, rounds=2)

    await keeper.run()

    assert calls == 2
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED


async def test_a_keeper_stopped_in_the_moment_it_wakes_still_stops(paper: PaperOptions) -> None:  # noqa: F811
    """A stop that lands just as a nudge ends the rest must end the task, never be swallowed by it."""
    keeper = DeadlineKeeper(repo=paper.repo, trade=paper.venue, off_loop=run_inline)
    keeper.start()
    await asyncio.sleep(0.01)
    keeper.nudge()

    await asyncio.wait_for(keeper.stop(), timeout=2)

    assert keeper._task is None


# ── a restart ────────────────────────────────────────────────────────────


async def test_a_restart_before_the_deadline_cancels_the_resting_entry_at_once(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    paper.wait(WAIT_MS // 2)

    await paper.restart()

    # No entry outlives its run: the restart stopped the run, so the entry goes now, not at its deadline.
    assert paper.repo.active_run("a") is None
    [(why, asked_at)] = paper.cancel_requests(order_ref)
    assert why == "EXIT_ENTRY_TERMINAL_PROOF"
    assert asked_at < paper.outcome(order_ref).cancel_at_ms
    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED
    assert paper.set_aside_cents() == 0


async def test_a_restart_after_the_deadline_cancels_the_entry_the_dead_process_left_resting(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    paper.wait(WAIT_MS + 40_000)

    await paper.restart()

    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED
    assert len(paper.cancel_requests(order_ref)) == 1
    # Nothing is left for the new process's keeper to find.
    assert (await paper.round()).entries.ended == ()


async def test_a_cancel_left_uncertain_at_a_restart_is_proven_by_the_keepers_first_round(paper: PaperOptions) -> None:  # noqa: F811
    order_ref = await paper.resting()
    paper.wait(WAIT_MS + 1_000)
    paper.venue.cancel_error = BrokerUnavailable("Alpaca did not answer", broker="alpaca")
    await paper.restart()
    # The startup pass asked, Alpaca did not answer, and its open orders still list the entry.
    assert paper.outcome(order_ref).standing is EntryStanding.CANCELLATION_REQUESTED
    assert len(paper.recorded(order_ref, "ORDER_CANCEL_UNCERTAIN")) >= 1

    paper.venue.cancel_error = None
    await paper.round()

    assert paper.outcome(order_ref).standing is EntryStanding.UNFILLED


# ── where it is composed ─────────────────────────────────────────────────


async def test_a_dry_runs_clerk_has_a_keeper_running_and_stores_a_deadline_it_never_needs(dry_run: _DryRun) -> None:  # noqa: F811
    runtime = get_clerk_runtime(dry_run.binding.sealed_account_id or "")
    assert runtime is not None and isinstance(runtime.deadline_keeper, DeadlineKeeper)
    assert runtime.deadline_keeper._task is not None and not runtime.deadline_keeper._task.done()

    bought = await dry_run.enter(dry_run.evidence())

    # A simulation fills at once: the order has ended before any deadline could matter.
    [order_ref] = bought.child_order_refs
    done = await runtime.deadline_keeper.tick()
    assert done.entries.ended == () and done.wake_in_ms == IDLE_WAKE_MS
    assert dry_run.repo.order(order_ref).broker_state == "filled"


async def test_the_keeper_starts_with_the_other_taps_and_is_stopped_before_the_sweep() -> None:
    calls: list[str] = []
    runtime = ActiveClerkRuntime(
        authority_kind="sqlite",
        sweep=_FakeTap("sweep", calls),
        hold_sync=_FakeTap("hold_sync", calls),
        envelope_sync=_FakeTap("envelope_sync", calls),
        deadline_keeper=_FakeTap("deadline_keeper", calls),
    )

    runtime.start_background_taps()
    await runtime.close()

    assert calls == [
        "envelope_sync.start", "hold_sync.start", "deadline_keeper.start",
        "envelope_sync.stop", "hold_sync.stop", "deadline_keeper.stop", "sweep.stop",
    ]
    assert runtime.deadline_keeper is None
