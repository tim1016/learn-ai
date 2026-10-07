"""The scheduled close on a Dry Run, and what it costs an account that trades shares only (#2913).

**A Dry Run.** Its Clerk has no pass on a timer, so its keeper is what closes
a running bot's contract on its expiry day. The simulation prices that close
as it prices every close: from the newest quote kept for the contract, saying
how old it is. The scene is ``test_option_entry_and_exit``'s: one SPY call
expiring Friday 2026-09-18, bought on the 8th at $1.94 a share.

**A stock-only account.** The keeper's round and the pass's step read what is
held once, find no recorded contract, and stop there: nothing is written, no
lock is taken, and no row of the custody log is read however long it is.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite import reads, reconcile
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.deadline_keeper import IDLE_WAKE_MS, DeadlineKeeper
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.off_loop import run_inline
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_uncertain
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.scheduled_close import (
    CloseStanding,
    HeldContracts,
    ScheduledCloseRound,
    _accept_while_still_owed,
    drive_scheduled_closes,
    held_contracts,
    scheduled_close_state,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_account_hold, resolve_account_hold
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_NOT_FLAT_REASON_CODE,
    SCHEDULED_CLOSE_MISSED_REASON_CODE,
    STREAM_HEALTH_HOLD_REASON_CODE,
)
from app.broker.contract.models import OrderSide
from app.options.simulated_fill import SimulatedFillModel
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.sqlite.conftest import (
    FIXTURE_RTH_MS,
    _broker_leg,
    _FakeReadPort,
    _FakeTradePort,
    _make_held_position,
    _TestClock,
    _walk_clock_to,
)
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    PaperOptions,
    paper,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import (
    RUN,
    SID,
    SYMBOL,
    _DryRun,
    _quote,
    dry_run,  # noqa: F401 -- pytest fixture, used by name
)

EXPIRY = date(2026, 9, 18)
# 45 minutes before the four o'clock close of a full day.
DUE = et_minute_of_day_ms(EXPIRY, 15 * 60 + 15)
CLOSE = et_minute_of_day_ms(EXPIRY, 16 * 60)


def _keeper(account: _DryRun) -> DeadlineKeeper:
    """The Dry Run account's own keeper, as its composition built it. A test asks it for each round."""
    runtime = get_clerk_runtime(account.binding.sealed_account_id or "")
    assert runtime is not None and isinstance(runtime.deadline_keeper, DeadlineKeeper)
    return runtime.deadline_keeper


def _until(account: _DryRun, at_ms: int) -> None:
    """Days pass. The Clerk's hold on its account is taken up again where it left it."""
    assert at_ms >= account.clock()
    account.clock.value = at_ms
    account.repo.revive_execution_lease()


def _hand_over(account: _DryRun, bid: str, ask: str, *, age_ms: int = 200) -> None:
    """The newest quote held for the contract is stated to the account's kept quotes, as the Clerk's keeping does."""
    account.hand_over({SYMBOL: _quote(bid, ask, received_ms=account.clock() - age_ms)})


def _notice(account: _DryRun, reason_code: str = SCHEDULED_CLOSE_MISSED_REASON_CODE) -> dict | None:
    return account.repo.active_uncertainty(scope="CUSTODY_SUBJECT", reason_code=reason_code, strategy_instance_id=SID)


# ── a Dry Run ────────────────────────────────────────────────────────────


async def test_a_running_dry_runs_contract_is_closed_at_its_time_from_the_newest_quote_kept_saying_its_age(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    _until(dry_run, DUE - 60_000)
    _hand_over(dry_run, "2.10", "2.16")
    keeper = _keeper(dry_run)

    early = await keeper.tick()
    assert early.closes.taken == () and early.wake_in_ms == IDLE_WAKE_MS and dry_run.held() == {SYMBOL: 1.0}

    _until(dry_run, DUE)
    done = await keeper.tick()

    # Sold while the run is still the bot's, at the bid kept a minute before: one contract at $2.10.
    assert dry_run.repo.active_run(SID) is not None
    assert len(done.closes.taken) == 1 and dry_run.held() == {}
    assert dry_run.fills()[-1] == (SYMBOL, "sell", 1.0, 2.10, 100)
    [sale] = [fill for fill in dry_run.clerk.synthesized_option_fills().values() if fill.request.side is OrderSide.SELL]
    assert (sale.fill_model, sale.fill_price, sale.quote_age_ms) == (
        SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal("2.10"), 60_200
    )
    [state] = scheduled_close_state(dry_run.repo, SID)
    assert (state.standing, state.taken_at_ms, state.late) == (CloseStanding.FLAT, DUE, False)


async def test_a_dry_runs_keeper_sleeps_to_the_close_that_comes_due_and_to_the_end_of_its_session(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    _hand_over(dry_run, "0", "0.05")
    keeper = _keeper(dry_run)

    _until(dry_run, DUE - 1_300)
    before = await keeper.tick()
    _until(dry_run, CLOSE - 700)
    after = await keeper.tick()

    # To the due time while the close is still to come; to the session's close once it is due and not made.
    assert before.wake_in_ms == 1_300 and after.wake_in_ms == 700


async def test_a_second_round_never_sells_a_dry_runs_contract_twice(dry_run: _DryRun) -> None:  # noqa: F811
    await dry_run.enter(dry_run.evidence())
    _until(dry_run, DUE)
    keeper = _keeper(dry_run)
    await keeper.tick()
    sold = [fill for fill in dry_run.fills() if fill[1] == "sell"]

    again = await keeper.tick()
    await dry_run.clerk.reconcile_once()

    assert len(sold) == 1 and [fill for fill in dry_run.fills() if fill[1] == "sell"] == sold
    assert again.closes.taken == () and dry_run.orders() == 2


async def test_with_nothing_bid_a_dry_runs_close_fails_and_the_sessions_end_records_it_as_missed(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    _until(dry_run, DUE)
    _hand_over(dry_run, "0", "0.05")
    keeper = _keeper(dry_run)

    done = await keeper.tick()

    # Taken, and not priced: the simulation sells nothing for nothing. The position is still open.
    assert len(done.closes.taken) == 1 and dry_run.held() == {SYMBOL: 1.0}
    assert _notice(dry_run, EXIT_NOT_FLAT_REASON_CODE) is not None and _notice(dry_run) is None
    [failed] = scheduled_close_state(dry_run.repo, SID)
    assert failed.standing is CloseStanding.FAILED

    _until(dry_run, CLOSE)
    ended = await keeper.tick()

    assert ended.closes.missed == ((SID, SYMBOL),) and dry_run.held() == {SYMBOL: 1.0}
    notice = _notice(dry_run)
    assert notice is not None and SYMBOL in notice["explanation"] and "Dry Run" in notice["explanation"]
    [missed] = scheduled_close_state(dry_run.repo, SID)
    assert (missed.standing, missed.taken_at_ms) == (CloseStanding.MISSED, DUE)
    assert [fill for fill in dry_run.fills() if fill[1] == "sell"] == []


async def test_a_running_dry_runs_failed_close_is_taken_again_thirty_seconds_apart_and_sells_once_a_bid_is_kept(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Nothing is bid at the due instant. A minute later a good quote is kept, and the close then sells."""
    await dry_run.enter(dry_run.evidence())
    _until(dry_run, DUE)
    _hand_over(dry_run, "0", "0.05")
    keeper = _keeper(dry_run)
    first = await keeper.tick()
    assert len(first.closes.taken) == 1 and dry_run.held() == {SYMBOL: 1.0}

    # The keeper looks every five seconds. It takes the close again thirty seconds after the last try, no sooner.
    taken_again_at: list[int] = []
    for _ in range(11):
        dry_run.wait(5_000)
        if (await keeper.tick()).closes.taken:
            taken_again_at.append(dry_run.clock() - DUE)
    assert taken_again_at == [30_000] and dry_run.held() == {SYMBOL: 1.0}

    # Two seconds before the next try it takes nothing, and sleeps exactly to it.
    dry_run.wait(3_000)
    just_before = await keeper.tick()
    assert just_before.closes.taken == () and just_before.wake_in_ms == 2_000

    dry_run.wait(2_000)
    _hand_over(dry_run, "2.10", "2.16")
    sold = await keeper.tick()

    # Sold while the run is still the bot's, at the bid just kept. The close's own time is still the due time.
    assert len(sold.closes.taken) == 1 and dry_run.held() == {} and dry_run.repo.active_run(SID) is not None
    assert dry_run.fills()[-1] == (SYMBOL, "sell", 1.0, 2.10, 100)
    [state] = scheduled_close_state(dry_run.repo, SID)
    assert (state.standing, state.taken_at_ms) == (CloseStanding.FLAT, DUE)


async def test_a_dry_runs_failed_close_is_tried_no_sooner_than_thirty_seconds_after_the_bots_own_failed_exit(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    _until(dry_run, DUE)
    _hand_over(dry_run, "0", "0.05")
    keeper = _keeper(dry_run)
    await keeper.tick()
    dry_run.wait(20_000)
    await dry_run.exit()
    orders_made = dry_run.orders()

    dry_run.wait(10_000)
    thirty_after_the_close = await keeper.tick()
    orders_then = dry_run.orders()
    dry_run.wait(20_000)
    thirty_after_the_bots_exit = await keeper.tick()

    # Counted from the bot's newest exit, whoever made it: two tries are never closer than the wait.
    assert thirty_after_the_close.closes.taken == () and orders_then == orders_made
    assert len(thirty_after_the_bots_exit.closes.taken) == 1 and dry_run.orders() == orders_made + 1


async def test_a_failed_close_read_as_ready_is_not_taken_again_once_the_bot_has_made_a_newer_exit(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Asked again in the run that takes it: what was read a moment ago is not what decides."""
    await dry_run.enter(dry_run.evidence())
    _until(dry_run, DUE)
    _hand_over(dry_run, "0", "0.05")
    await _keeper(dry_run).tick()
    dry_run.wait(30_000)
    [ready] = held_contracts(dry_run.repo, dry_run.clock()).held
    assert ready.to_be_taken_again(dry_run.clock())
    await dry_run.exit()
    orders_made = dry_run.orders()

    taken = _accept_while_still_owed(dry_run.repo, ready)

    assert taken is None and dry_run.orders() == orders_made


async def test_a_dry_runs_failed_close_is_not_taken_again_once_its_session_has_ended(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    _until(dry_run, CLOSE - 20_000)
    _hand_over(dry_run, "0", "0.05")
    keeper = _keeper(dry_run)
    await keeper.tick()

    _until(dry_run, CLOSE + 10_000)
    _hand_over(dry_run, "2.10", "2.16")
    after_the_close = [await keeper.tick() for _ in range(2)]

    # Thirty seconds have passed and a bid is kept, but the session the close was owed in is over.
    assert [looked.closes.taken for looked in after_the_close] == [(), ()]
    assert dry_run.held() == {SYMBOL: 1.0} and _notice(dry_run) is not None


async def test_a_dry_runs_keeper_drives_on_a_close_it_took_and_could_not_send_since_no_pass_will(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """A hold on the account refuses reductions when the close comes due. The close is taken and waits."""
    await dry_run.enter(dry_run.evidence())
    _until(dry_run, DUE)
    _hand_over(dry_run, "2.10", "2.16")
    raise_account_hold(
        dry_run.repo, reason_code=STREAM_HEALTH_HOLD_REASON_CODE, evidence_refs=["execution: the order stream is down"]
    )
    keeper = _keeper(dry_run)

    held_up = await keeper.tick()
    assert len(held_up.closes.taken) == 1 and held_up.closes.deferred == held_up.closes.taken
    assert dry_run.held() == {SYMBOL: 1.0} and dry_run.repo.active_exit_for_strategy(SID) is not None

    resolve_account_hold(
        dry_run.repo, reason_code=STREAM_HEALTH_HOLD_REASON_CODE, summary_code="ACCOUNT_HOLD_RESOLVED_BY_STREAM_RECOVERY"
    )
    dry_run.wait(5_000)
    done = await keeper.tick()

    # The same EXIT, driven on by the keeper: taken once, and now sold.
    assert done.closes.taken == () and done.closes.driven == held_up.closes.taken
    assert dry_run.held() == {} and dry_run.fills()[-1] == (SYMBOL, "sell", 1.0, 2.10, 100)


async def test_a_dry_runs_keeper_drives_on_no_exit_but_a_scheduled_close(dry_run: _DryRun) -> None:  # noqa: F811
    """The bot's own exit was taken and nothing has driven it. That exit is not the keeper's to send."""
    bought = await dry_run.enter(dry_run.evidence())
    [order_ref] = bought.child_order_refs
    _until(dry_run, DUE - 60_000)
    _hand_over(dry_run, "2.10", "2.16")
    accept_exit(
        dry_run.repo, account_id=dry_run.repo.account_id, strategy_instance_id=SID, decision_id="the-bots-own-exit",
        lifecycle_run_id=RUN, entry_order_ref=order_ref,
    )
    _until(dry_run, DUE)

    done = await _keeper(dry_run).tick()

    assert (done.closes.taken, done.closes.driven, done.closes.deferred) == ((), (), ())
    assert dry_run.held() == {SYMBOL: 1.0} and dry_run.orders() == 1


async def test_a_stopped_dry_run_whose_run_end_close_sold_its_contract_owes_no_close(dry_run: _DryRun) -> None:  # noqa: F811
    await dry_run.enter(dry_run.evidence())
    _until(dry_run, DUE - 60 * 60_000)
    _hand_over(dry_run, "2.10", "2.16")
    await dry_run.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN)
    await dry_run.clerk.reconcile_once()
    assert dry_run.held() == {}

    _until(dry_run, DUE)
    done = await _keeper(dry_run).tick()
    await dry_run.clerk.reconcile_once()

    assert done.closes == ScheduledCloseRound() and dry_run.orders() == 2
    assert scheduled_close_state(dry_run.repo, SID) == ()


# ── an account that trades shares only ───────────────────────────────────


async def _a_stock_account_holding_shares(root: Path, *, transitions: int) -> ClerkSqliteRepository:
    """A stock-only account whose one bot holds ten shares, and ``transitions`` more records in its log."""
    clock = _TestClock(FIXTURE_RTH_MS)
    repo = ClerkSqliteRepository.initialize(account_id="PA-STOCK", artifacts_root=root, clock=clock)
    repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
    submit_start_run(repo, account_id="PA-STOCK", strategy_instance_id="spy-bot", lifecycle_run_id="run-1")
    order_ref = await _make_held_position(
        repo, account_id="PA-STOCK", strategy_instance_id="spy-bot", run_id="run-1", quantity=10.0
    )
    assert repo.position("spy-bot", "SPY") == 10.0
    order = repo.order(order_ref)
    assert order is not None
    for number in range(transitions):
        fold_uncertain(repo, effect_operation_id=order.effect_operation_id, order_ref=order.order_ref, why=f"n{number}")
    return repo


class _StepsCounted:
    """How many steps SQLite's machine took on a store's connection: it grows with every row a query reads."""

    def __init__(self, repo: ClerkSqliteRepository) -> None:
        self._repo = repo
        self.steps = 0

    def __enter__(self) -> _StepsCounted:
        self._repo._conn.set_progress_handler(self._count, 1)
        return self

    def __exit__(self, *_raised: object) -> None:
        self._repo._conn.set_progress_handler(None, 1)

    def _count(self) -> int:
        self.steps += 1
        return 0


async def test_a_stock_only_accounts_books_are_never_read_for_a_close_however_long_their_log(tmp_path: Path) -> None:
    """The keeper's round with its close step, the pass's step and the worklist read cost a stock account the same
    few steps whether its log holds a handful of records or three thousand more."""
    costs: dict[int, tuple[int, int, int]] = {}
    for transitions in (0, 3_000):
        repo = await _a_stock_account_holding_shares(tmp_path / str(transitions), transitions=transitions)
        try:
            written = repo.last_custody_sequence()
            venue, intake = _FakeTradePort(), ReentrantAsyncLock()
            keeper = DeadlineKeeper(
                repo=repo, trade=venue, intake=intake, pricing=UNPRICEABLE_RECOVERY, off_loop=run_inline
            )
            with _StepsCounted(repo) as the_round:
                done = await keeper.tick()
            with _StepsCounted(repo) as the_step:
                stepped = await drive_scheduled_closes(repo, trade=venue, intake=intake, pricing=UNPRICEABLE_RECOVERY)
            with _StepsCounted(repo) as the_read:
                read = held_contracts(repo, repo.clock())
            assert done.closes == stepped == ScheduledCloseRound() and done.wake_in_ms == IDLE_WAKE_MS
            # Nothing held in a contract, nothing unreadable, no notice: the shares are not even looked at twice.
            assert read == HeldContracts() and repo.last_custody_sequence() == written
            costs[transitions] = (the_round.steps, the_step.steps, the_read.steps)
        finally:
            repo.close()

    assert costs[0] == costs[3_000]
    # A few dozen steps each: one look at the one position, one at the empty table of contracts, one at the notices.
    assert all(0 < steps < 200 for steps in costs[0]), costs[0]


# ── an account that holds a contract ─────────────────────────────────────


async def _buys_one_call(paper: PaperOptions, trip: int) -> None:  # noqa: F811
    bought = await paper.resting("a")
    await paper.stream_fill(bought, contracts=1, filled_in_all=1, price="1.94", execution_id=f"buy-{trip}")


async def _sells_it_again(paper: PaperOptions, trip: int) -> None:  # noqa: F811
    await paper.exit("a")
    sold = [order.order_ref for order in paper.repo.orders_for_strategy("a") if order.role == "REDUCING"][-1]
    await paper.stream_fill(sold, contracts=1, filled_in_all=1, price="2.10", execution_id=f"sell-{trip}")
    await paper.pass_()
    assert paper.held() == {}
    # The account is read again, as its own cadence reads it, before the next entry.
    paper.wait(1_000)
    paper.account.account_observed_at_ms = paper.clock()
    assert await paper.sync.tick() == "observed"


async def test_a_bot_holding_a_contract_that_is_not_due_today_costs_the_same_few_steps_however_long_its_history(
    paper: PaperOptions,  # noqa: F811
) -> None:
    """Ten days before its expiry a held call owes nothing, and that is told from the position alone.

    Counted in the steps SQLite's machine takes, which grow with every row
    read. The bot holds one call after one round trip, and again after
    twelve: the step and the read cost the same both times. The same bot is
    then counted on its expiry day, when the entries behind its holding are
    read: that day's cost does grow with its history, and is held under a
    bound for each past entry.
    """

    async def step() -> ScheduledCloseRound:
        return await drive_scheduled_closes(
            paper.repo, trade=paper.venue, intake=paper.clerk.intake, pricing=paper.clerk.recovery_pricing
        )

    costs: dict[int, tuple[int, int]] = {}
    for trip in range(13):
        await _buys_one_call(paper, trip)
        if trip in (1, 12):
            with _StepsCounted(paper.repo) as the_step:
                stepped = await step()
            with _StepsCounted(paper.repo) as the_read:
                read = held_contracts(paper.repo, paper.clock())
            # Held, and passed over without one entry read: nothing is owed and nothing is to change.
            assert stepped == ScheduledCloseRound()
            assert (read.held, read.holders, read.unread) == ((), frozenset({"a"}), frozenset())
            costs[trip] = (the_step.steps, the_read.steps)
        if trip < 12:
            await _sells_it_again(paper, trip)

    assert costs[1] == costs[12]
    assert all(0 < steps < 100 for steps in costs[12]), costs[12]

    paper.clock.value = et_minute_of_day_ms(EXPIRY, 10 * 60)
    paper.repo.revive_execution_lease()
    with _StepsCounted(paper.repo) as on_its_day:
        [due_today] = held_contracts(paper.repo, paper.clock()).held

    # On its expiry day the close is owed, and the entries behind the holding are read to say so.
    assert due_today.owed is not None and due_today.owed.due_ms == DUE
    assert costs[12][1] < on_its_day.steps < 400 * 13, on_its_day.steps


def _plan(repo: ClerkSqliteRepository, sql: str, params: tuple[object, ...] = ()) -> list[str]:
    with repo.write_fence() as conn:
        return [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()]


def test_what_is_held_is_read_from_the_positions_and_never_scans_the_custody_log(paper: PaperOptions) -> None:  # noqa: F811
    plan = _plan(paper.repo, reads.HELD_OPTION_CONTRACTS_SQL)

    # It starts at the positions, and reaches the recorded contracts by their key. No other table is read.
    read = [step for step in plan if step.startswith(("SCAN", "SEARCH"))]
    assert [step.split()[1] for step in read] == ["p", "contract"]
    assert read[1].startswith("SEARCH contract USING") and not any(step.startswith("SCAN contract") for step in plan)


async def test_on_books_holding_no_option_contract_the_step_takes_no_lock(tmp_path: Path) -> None:
    repo = await _a_stock_account_holding_shares(tmp_path, transitions=0)

    class _LockThatMustNotBeTaken(ReentrantAsyncLock):
        async def off_loop(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("the intake lock was taken on books that hold no option contract")

    try:
        done = await drive_scheduled_closes(
            repo, trade=_FakeTradePort(), intake=_LockThatMustNotBeTaken(), pricing=UNPRICEABLE_RECOVERY
        )

        assert done == ScheduledCloseRound()
    finally:
        repo.close()


async def _a_stopped_stock_run_with_a_working_entry(root: Path) -> list[tuple[str, str, str]]:
    """One pass over a stock-only account whose stopped run left an entry working; the log it wrote."""
    clock = _TestClock(FIXTURE_RTH_MS)
    repo = ClerkSqliteRepository.initialize(account_id="PA-STOCK", artifacts_root=root, clock=clock)
    try:
        repo.register_strategy_instance(strategy_instance_id="spy-bot", symbol="SPY", config_hash="h1")
        submit_start_run(repo, account_id="PA-STOCK", strategy_instance_id="spy-bot", lifecycle_run_id="run-1")
        venue = _FakeTradePort()
        await submit_enter(
            repo, account_id="PA-STOCK", strategy_instance_id="spy-bot", decision_id="d-1", lifecycle_run_id="run-1",
            leg=_broker_leg(), trade=venue,
        )
        submit_stop_run(repo, account_id="PA-STOCK", strategy_instance_id="spy-bot", lifecycle_run_id="run-1")
        _walk_clock_to(repo, clock() + 60_000)
        await reconcile_account(repo, read=_FakeReadPort(), trade=venue, pricing=UNPRICEABLE_RECOVERY)
        with repo.write_fence() as conn:
            rows = conn.execute(
                "SELECT transition_kind, summary_code, facts_json FROM custody_transitions ORDER BY sequence"
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]
    finally:
        repo.close()


async def test_a_stock_only_account_is_recorded_the_same_with_the_step_and_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_the_step = await _a_stopped_stock_run_with_a_working_entry(tmp_path / "with")

    async def no_step(*_args: object, **_kwargs: object) -> ScheduledCloseRound:
        return ScheduledCloseRound()

    monkeypatch.setattr(reconcile, "drive_scheduled_closes", no_step)
    without_it = await _a_stopped_stock_run_with_a_working_entry(tmp_path / "without")

    assert with_the_step == without_it
    # The scenario is not an empty one: the pass cancelled the stopped run's working entry.
    assert "ORDER_CANCEL_REQUESTED" in [kind for kind, _summary, _facts in with_the_step]
