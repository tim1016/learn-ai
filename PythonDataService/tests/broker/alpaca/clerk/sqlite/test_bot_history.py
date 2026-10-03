"""Bot history's custody read: every bot, run by run (#2574).

A seeded Clerk database is the golden fixture: each run's transactions and
orders are counted exactly, the bot's totals equal ``project_bot_results``'
``trade_count``, and its result and fees are the same figures the money
authority reports -- per bot, never split across runs.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY, ConfirmedRecoveryLimit
from app.broker.alpaca.clerk.sqlite.bot_history import (
    CustodyHistory,
    OrderCounts,
    RunFacts,
    is_owner_flatten_decision,
    read_custody_history,
)
from app.broker.alpaca.clerk.sqlite.budget_projection import RevisionMemo, project_bot_results
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.exit import resolve_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import EXIT_REDRIVE_DECISION_PREFIX
from app.broker.alpaca.clerk.sqlite.exit_watchdog import BrokerSymbolView
from app.broker.alpaca.clerk.sqlite.facts import OrderSubmitAckedFacts
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import ReentrantAsyncLock, SqliteAlpacaClerkFacade, _durable_decision_id
from app.broker.alpaca.clerk.sqlite.safe_flatten_execution import SafeFlattenExecutionError, SafeFlattenResult
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.errors import BrokerOrderRejected
from app.broker.contract.models import BrokerOrderEvent, BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite import test_safe_flatten_execution as safe_flatten
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _covering_read, _TestClock, _walk_clock_to
from tests.broker.alpaca.clerk.sqlite.test_budget_claims import _record_sale
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS, _deploy, _gate, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice
from tests.broker.alpaca.clerk.sqlite.test_exit_send_session import (
    _acked,
    _live_touch,
    redrive_or_escalate_stale_exits,
)
from tests.broker.alpaca.clerk.sqlite.test_safe_flatten_execution import (
    crashed_with_exposure,  # noqa: F401 — the held-and-stopped repository the flatten test reuses
)


def _enter(repo: ClerkSqliteRepository, sid: str, run: str, key: str, **gate: object) -> EnterSubmission:
    return accept_enter(
        repo, account_id=repo.account_id, strategy_instance_id=sid, decision_id=key,
        lifecycle_run_id=run, leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        reference_price=100, **gate,  # type: ignore[arg-type]
    )


def _ack(repo: ClerkSqliteRepository, accepted: EnterSubmission, state: str) -> None:
    """The broker reports the order, in ``state``."""
    repo.append_transition(TransitionInput(
        strategy_instance_id=accepted.command.strategy_instance_id, run_id=accepted.command.run_id,
        command_id=accepted.command.command_id, effect_operation_id=accepted.effect_operation_id,
        order_ref=accepted.order_ref, broker_order_id=f"broker-{accepted.order_ref}", broker_state=state,
        transition_kind="ORDER_SUBMIT_ACKED", custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
        operation_state="in_progress", source_event_at_ms=repo.clock(), clerk_observed_at_ms=repo.clock(),
        summary_code="ORDER_SUBMIT_ACKED", facts_json=OrderSubmitAckedFacts().to_facts_json(),
    ))


@pytest.fixture
def old_bot(tmp_path: Path):
    """A bot the retired Resume ran twice: two runs on one registration.

    Run 1 bought 1 SPY at 100 and sold it at 110 on one filled order's
    lineage. Run 2 sent an order the broker rejected, then bought 1 SPY at
    105 on a second order.
    """
    repo = ClerkSqliteRepository.initialize(account_id="OLD-PAPER", artifacts_root=tmp_path, clock=_TestClock(NOON))
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    repo.register_strategy_instance(strategy_instance_id="old", symbol="SPY", config_hash="seal-old", exit_terms=TERMS)
    submit_start_run(repo, account_id=repo.account_id, strategy_instance_id="old", lifecycle_run_id="r1", clock=repo.clock)
    traded = _enter(repo, "old", "r1", "enter-1")
    _ack(repo, traded, "filled")
    _append_slice(repo, traded, execution_id="buy-1", quantity=1, source_event_at_ms=NOON - 10, fee=0)
    _record_sale(repo, traded, key="sell-1", price=110, at_ms=NOON - 9)
    submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="old", lifecycle_run_id="r1", clock=repo.clock)
    submit_start_run(repo, account_id=repo.account_id, strategy_instance_id="old", lifecycle_run_id="r2", clock=repo.clock)
    refused = _enter(repo, "old", "r2", "enter-2")
    _ack(repo, refused, "rejected")
    bought = _enter(repo, "old", "r2", "enter-3")
    _ack(repo, bought, "filled")
    _append_slice(repo, bought, execution_id="buy-2", quantity=1, source_event_at_ms=NOON - 5, price=105, fee=0)
    submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="old", lifecycle_run_id="r2", clock=repo.clock)
    yield repo
    repo.close()


def test_each_run_counts_its_own_transactions_and_orders_exactly(old_bot: ClerkSqliteRepository) -> None:
    history = old_bot.bot_history()

    (bot,) = history.bots
    newest, oldest = bot.runs
    assert (oldest.lifecycle_run_id, newest.lifecycle_run_id) == ("r1", "r2")
    assert oldest.transactions == 2
    assert oldest.orders == OrderCounts(sent=1, filled=1, cancelled=0, rejected=0)
    assert newest.transactions == 1
    assert newest.orders == OrderCounts(sent=2, filled=1, cancelled=0, rejected=1)
    # Every run stopped, and says when.
    assert oldest.stopped_at_ms is not None and newest.stopped_at_ms is not None
    assert not oldest.active and not newest.active


def test_the_bots_totals_and_money_are_the_money_authoritys_own(old_bot: ClerkSqliteRepository) -> None:
    with old_bot._write_lock:
        fees = old_bot.fee_attribution(now_ms=NOON)
        results = project_bot_results(old_bot._conn, fees=fees, strategy_instance_ids=["old"])

    (bot,) = old_bot.bot_history().bots
    assert bot.transactions == results["old"].trade_count == 3
    assert bot.orders == OrderCounts(sent=3, filled=2, cancelled=0, rejected=1)
    assert bot.result == results["old"].result
    assert bot.fees == fees.total_for(bot_subject_id("old"))
    assert bot.committed_cents is None  # deployed before budgets


def test_a_budgeted_bot_carries_its_budget_and_a_cleared_one_stays_readable(tmp_path: Path) -> None:
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo, "a", 25_000)
        entry = _enter(repo, "a", "run-a", "enter-a", envelope=_gate())
        _ack(repo, entry, "canceled")
        submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a", clock=repo.clock)
        from app.services.bot_lifecycle_projection import SqliteAlpacaLifecycleAuthority

        SqliteAlpacaLifecycleAuthority(repo).retire("a", NOON + 5, "Cleared from Home")

        bots = {bot.strategy_instance_id: bot for bot in repo.bot_history().bots}
        cleared = bots["a"]
        assert cleared.retired_at_ms == NOON + 5
        assert cleared.committed_cents == 25_000
        assert cleared.orders == OrderCounts(sent=1, filled=0, cancelled=1, rejected=0)
        assert cleared.transactions == 0
        # Registered but never deployed: no run, no budget, and still listed.
        assert bots["b"].runs == () and bots["b"].committed_cents is None
    finally:
        repo.close()


def test_unvouched_fees_leave_result_and_fees_unknown_never_zero(old_bot: ClerkSqliteRepository) -> None:
    history = read_custody_history(old_bot.db_path, now_ms=NOON, fee_evidence_checked_at_ms=None)

    (bot,) = history.bots
    assert history.money_unavailable is not None and "Fee evidence" in history.money_unavailable
    assert bot.result is None and bot.fees is None
    # The counts need no fee evidence.
    assert bot.transactions == 3


async def _stopped_holding_ten(repo: ClerkSqliteRepository) -> None:
    """The owner stops a bot holding 10 SPY."""
    await safe_flatten._held_position(repo)
    submit_stop_run(
        repo, account_id=safe_flatten.ACCOUNT_ID, strategy_instance_id=safe_flatten.SID,
        lifecycle_run_id=safe_flatten.RUN_ID, operator_reason="operator_stop",
    )


async def _owner_flattens(repo: ClerkSqliteRepository, trade: safe_flatten._FakeTrade) -> SafeFlattenResult:
    """The owner flattens the stopped bot through the Clerk's own safe flatten."""
    facade = SqliteAlpacaClerkFacade(
        repo=repo, read=safe_flatten._FakeRead(positions=[safe_flatten._position("SPY", quantity=10.0)]),
        trade=trade, account_mode="paper",
    )
    return await facade.execute_safe_flatten(plan=await safe_flatten._reconciled_flatten_plan(repo), reason="owner flatten")


async def _broker_reports(repo: ClerkSqliteRepository, flatten: SafeFlattenResult, *, sold: int, state: str) -> None:
    """The broker reports the flatten's order ``state``, having sold ``sold`` of its 10 SPY."""
    (order,) = flatten.orders
    await _broker_reports_order(
        repo, order.order_ref, order.effect_operation_id, quantity=10, sold=sold, state=state,
        execution_id="flatten-exec-1",
    )


async def _broker_reports_order(
    repo: ClerkSqliteRepository, order_ref: str, effect_operation_id: str,
    *, quantity: int, sold: int, state: str, execution_id: str,
) -> None:
    """The broker reports a sell of ``quantity`` SPY in ``state``, ``sold`` of it filled."""
    reported = safe_flatten._broker_order(
        order_ref, order_id=f"bo-{order_ref}", status=state, side="sell",
        quantity=float(quantity), filled_quantity=sold, filled_avg_price=100.0,
    )
    sink = SqliteTradeUpdateEvidenceSink(repo=repo, intake=ReentrantAsyncLock(), reconciler=safe_flatten._NoReconciler())
    await sink.record_lifecycle_event(
        client_order_id=order_ref,
        event=BrokerOrderEvent(
            event_type="fill" if sold == quantity else "partial_fill", occurred_at_ms=repo.clock(),
            price=100, quantity=sold, execution_id=execution_id,
        ),
        event_key=f"execution:{execution_id}", order=reported,
        recovery_source=None, recovery_window_limit=None,
    )
    fold_order_evidence(repo, effect_operation_id=effect_operation_id, order=reported)


def _only_run(repo: ClerkSqliteRepository) -> RunFacts:
    (run,) = repo.bot_history().bots[0].runs
    return run


async def test_a_run_reads_as_flattened_only_once_the_owners_flatten_sold(
    crashed_with_exposure,  # noqa: F811 — the imported fixture
) -> None:
    """The owner stops a bot holding 10 SPY and flattens it: the run reads
    as flattened once the broker reports the flatten's order filled and the
    Clerk holds its fill -- never while it is merely accepted."""
    repo, _clock = crashed_with_exposure
    await _stopped_holding_ten(repo)
    stopped = _only_run(repo)

    flatten = await _owner_flattens(repo, safe_flatten._FakeTrade())
    accepted = _only_run(repo)
    await _broker_reports(repo, flatten, sold=10, state="filled")

    assert not stopped.flattened
    assert not accepted.flattened
    assert _only_run(repo).flattened


@pytest.mark.parametrize("state", ["partially_filled", "expired"])
async def test_a_flatten_that_sold_only_part_of_the_position_is_not_a_flatten(
    crashed_with_exposure,  # noqa: F811 — the imported fixture
    state: str,
) -> None:
    """A flatten that sold 4 of 10 SPY, still working or expired with the
    rest unsold, leaves the bot holding 6: the run reads as stopped, not
    flattened, beside the bot still holding."""
    repo, _clock = crashed_with_exposure
    await _stopped_holding_ten(repo)
    flatten = await _owner_flattens(repo, safe_flatten._FakeTrade())

    await _broker_reports(repo, flatten, sold=4, state=state)

    (bot,) = repo.bot_history().bots
    assert bot.holds_money
    assert not bot.runs[0].flattened


async def test_a_flatten_the_watchdogs_redrive_finished_reads_as_flattened(
    crashed_with_exposure,  # noqa: F811 — the imported fixture
) -> None:
    """#2615: the owner's flatten sold 4 of 10 SPY and expired; the stuck-EXIT
    watchdog re-drove the other 6, and they sold. The owner's flatten did
    finish, so the run reads as flattened -- it used to read "Stopped by you"."""
    repo, _clock = crashed_with_exposure
    await _stopped_holding_ten(repo)
    flatten = await _owner_flattens(repo, safe_flatten._FakeTrade())
    await _broker_reports(repo, flatten, sold=4, state="expired")
    (order,) = flatten.orders
    await resolve_exit(repo, effect_operation_id=order.effect_operation_id, trade=_acked(), pricing=UNPRICEABLE_RECOVERY, read=_covering_read())
    _walk_clock_to(repo, repo.clock() + 10 * 60_000)
    await redrive_or_escalate_stale_exits(
        repo, trade=_acked(), intake=ReentrantAsyncLock(), pricing=_live_touch(),
        broker_symbol=lambda _symbol: BrokerSymbolView(6, 6, False, True),
    )
    (redrive,) = (
        row for row in repo.orders_for_strategy(safe_flatten.SID)
        if row.effect_operation_id.startswith(f"effect:{safe_flatten.SID}:{EXIT_REDRIVE_DECISION_PREFIX}")
    )
    re_driven = _only_run(repo)

    await _broker_reports_order(
        repo, redrive.order_ref, redrive.effect_operation_id, quantity=6, sold=6, state="filled",
        execution_id="redrive-exec-1",
    )

    assert not re_driven.flattened
    assert _only_run(repo).flattened
    assert not repo.bot_history().bots[0].holds_money


def _redrives(repo: ClerkSqliteRepository) -> list:
    """The watchdog's re-drive orders for the flattened bot, oldest first."""
    return sorted(
        (row for row in repo.orders_for_strategy(safe_flatten.SID)
         if row.effect_operation_id.startswith(f"effect:{safe_flatten.SID}:{EXIT_REDRIVE_DECISION_PREFIX}")),
        key=lambda row: row.effect_operation_id,
    )


async def _watchdog_redrives(repo: ClerkSqliteRepository, *, remaining: int) -> None:
    """The stuck-EXIT watchdog's next pass, once the episode has settled."""
    _walk_clock_to(repo, repo.clock() + 10 * 60_000)
    await redrive_or_escalate_stale_exits(
        repo, trade=_acked(), intake=ReentrantAsyncLock(), pricing=_live_touch(),
        broker_symbol=lambda _symbol: BrokerSymbolView(remaining, remaining, False, True),
    )


async def test_a_flatten_a_later_redrive_finished_reads_as_flattened(
    crashed_with_exposure,  # noqa: F811 — the imported fixture
) -> None:
    """#2615 review: a failed re-drive refreshes the episode with its own
    order, so the episode's row stops naming the owner's flatten. The flatten
    sold 4 of 10, the first re-drive 2 of 6, the second the last 4: the
    owner's flatten did finish."""
    repo, _clock = crashed_with_exposure
    await _stopped_holding_ten(repo)
    flatten = await _owner_flattens(repo, safe_flatten._FakeTrade())
    await _broker_reports(repo, flatten, sold=4, state="expired")
    (order,) = flatten.orders
    await resolve_exit(repo, effect_operation_id=order.effect_operation_id, trade=_acked(), pricing=UNPRICEABLE_RECOVERY, read=_covering_read())
    await _watchdog_redrives(repo, remaining=6)
    (first,) = _redrives(repo)
    await _broker_reports_order(
        repo, first.order_ref, first.effect_operation_id, quantity=6, sold=2, state="expired",
        execution_id="redrive-exec-1",
    )
    await resolve_exit(repo, effect_operation_id=first.effect_operation_id, trade=_acked(), pricing=UNPRICEABLE_RECOVERY, read=None)
    await _watchdog_redrives(repo, remaining=4)
    (_, second) = _redrives(repo)

    await _broker_reports_order(
        repo, second.order_ref, second.effect_operation_id, quantity=4, sold=4, state="filled",
        execution_id="redrive-exec-2",
    )

    assert not repo.bot_history().bots[0].holds_money
    assert _only_run(repo).flattened


def test_a_file_replaced_at_the_same_path_is_never_answered_from_the_old_ones_memo(
    old_bot: ClerkSqliteRepository, tmp_path: Path,
) -> None:
    """#2615 review: a file no running Clerk owns can be replaced where it
    lies (a cutover, a reset). At the same revision the memo answers again
    for the same file, but never for a new one."""
    path = tmp_path / "copy.db"
    source = sqlite3.connect(old_bot.db_path)
    target = sqlite3.connect(path)
    source.backup(target)
    source.close()
    memo: RevisionMemo[CustodyHistory] = RevisionMemo()
    first = read_custody_history(path, now_ms=NOON, fee_evidence_checked_at_ms=None, memo=memo)
    again = read_custody_history(path, now_ms=NOON, fee_evidence_checked_at_ms=None, memo=memo)
    target.execute("UPDATE control_meta SET db_identity_token = 'another-file' WHERE id = 1")
    target.commit()
    target.close()

    replaced = read_custody_history(path, now_ms=NOON, fee_evidence_checked_at_ms=None, memo=memo)

    assert again is first
    assert replaced is not first and replaced == first


async def test_a_flatten_the_broker_rejected_is_not_a_flatten(
    crashed_with_exposure,  # noqa: F811 — the imported fixture
) -> None:
    """The owner's flatten is accepted, then the broker refuses its order:
    nothing was sold, so the run is not flattened."""
    repo, _clock = crashed_with_exposure
    await _stopped_holding_ten(repo)

    with pytest.raises(SafeFlattenExecutionError, match="rejected"):
        await _owner_flattens(repo, safe_flatten._FakeTrade(submit_error=BrokerOrderRejected("insufficient buying power")))

    assert not _only_run(repo).flattened


async def test_a_flatten_limit_that_expired_unsent_is_not_a_flatten(
    crashed_with_exposure,  # noqa: F811 — the imported fixture
) -> None:
    """After hours the owner confirms a flatten limit (#2007) that cannot go
    out before the session ends: it expires unsent, so the run is not
    flattened."""
    repo, _clock = crashed_with_exposure
    facade, trade, current_context = await safe_flatten._stopped_facade_at(
        repo, safe_flatten._JUST_BEFORE_POST_CLOSE_MS, trade=safe_flatten._LookupOutageTrade(),
    )
    await safe_flatten._execute(
        facade, current_context,
        confirmed_limit=ConfirmedRecoveryLimit(
            limit_price=Decimal("99.95"), quote_observed_at_ms=safe_flatten._JUST_BEFORE_POST_CLOSE_MS,
        ),
    )
    active = repo.active_exit_for_strategy(safe_flatten.SID)
    assert active is not None
    _walk_clock_to(repo, safe_flatten._JUST_AFTER_POST_CLOSE_MS)
    trade.lookups_fail = False

    await resolve_exit(repo, effect_operation_id=active.effect_operation_id, trade=trade, pricing=UNPRICEABLE_RECOVERY, read=_covering_read())

    assert trade.submit_calls == []
    assert not _only_run(repo).flattened


@pytest.mark.parametrize(
    ("decision_id", "owner_flatten"),
    [
        ("recovery-flatten-0123456789abcdef", True),
        (_durable_decision_id("panel-flatten:key-1"), True),
        ("exit-redrive-0123456789ab-1", False),
        ("evaluation-42", False),
        (_durable_decision_id("strategy:exit"), False),
    ],
)
def test_only_the_owners_flattens_count_as_a_flatten(decision_id: str, owner_flatten: bool) -> None:
    """The safe flatten and the panel's flatten-and-stop are the owner's; a
    watchdog re-drive or a strategy's own EXIT is not."""
    assert is_owner_flatten_decision(decision_id) is owner_flatten
