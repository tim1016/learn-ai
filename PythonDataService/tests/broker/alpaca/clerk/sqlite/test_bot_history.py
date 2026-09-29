"""Bot history's custody read: every bot, run by run (#2574).

A seeded Clerk database is the golden fixture: each run's transactions and
orders are counted exactly, the bot's totals equal ``project_bot_results``'
``trade_count``, and its result and fees are the same figures the money
authority reports -- per bot, never split across runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.bot_history import OrderCounts, is_owner_flatten_decision, read_custody_history
from app.broker.alpaca.clerk.sqlite.budget_projection import project_bot_results
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.custody_subjects import bot_subject_id
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.facts import OrderSubmitAckedFacts
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade, _durable_decision_id
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite import test_safe_flatten_execution as safe_flatten
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
from tests.broker.alpaca.clerk.sqlite.test_budget_claims import _record_sale
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS, _deploy, _gate, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice
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


async def test_a_run_the_owner_flattened_after_its_stop_reads_as_flattened(
    crashed_with_exposure,  # noqa: F811 — the imported fixture
) -> None:
    """The owner stops a bot holding 10 SPY, then flattens it through the
    Clerk's own safe flatten: the reducing EXIT it records is the evidence,
    so the run reads as flattened only once that EXIT exists."""
    repo, _clock = crashed_with_exposure
    await safe_flatten._held_position(repo)
    submit_stop_run(
        repo, account_id=safe_flatten.ACCOUNT_ID, strategy_instance_id=safe_flatten.SID,
        lifecycle_run_id=safe_flatten.RUN_ID, operator_reason="operator_stop",
    )
    (stopped,) = repo.bot_history().bots[0].runs
    facade = SqliteAlpacaClerkFacade(
        repo=repo, read=safe_flatten._FakeRead(positions=[safe_flatten._position("SPY", quantity=10.0)]),
        trade=safe_flatten._FakeTrade(), account_mode="paper",
    )

    await facade.execute_safe_flatten(plan=await safe_flatten._reconciled_flatten_plan(repo), reason="owner flatten")

    (flattened,) = repo.bot_history().bots[0].runs
    assert not stopped.flattened
    assert flattened.flattened


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
