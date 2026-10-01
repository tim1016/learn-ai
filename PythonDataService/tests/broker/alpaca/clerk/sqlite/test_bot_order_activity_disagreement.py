"""A bot order whose account activity disagrees with what the Clerk recorded for it (#2791).

The stream never delivered the bot's fill, so the sweep's REST answer
credited its shares as a cumulative, and the fee-evidence producer then
recorded the executions Alpaca's account activity names for it (#2787).

- Activity priced against REST's rounded average used to need agreement to
  1e-9, so an ordinary multi-execution order stayed REST-only for ever: its
  fee coverage was incomplete and a budgeted account refused every bot
  entry. A gap under one price increment is now rounding (ADR 0036).
- A gap of one increment or more is a real disagreement. The order-total
  proof (#2346) closes its episode as before, and the fee population counts
  the executions it kept quarantined, so entries are admitted while the
  account's P&L coverage still asks for attention.
- An activity that contradicts an execution the order recorded is raised
  as a coverage conflict once, which the bot path used to drop silently.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.activity_executions import BOT_ORDER, record_activity_executions
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.execution_coverage import order_total_retained_exacts_explain_cumulative
from app.broker.alpaca.clerk.sqlite.fee_evidence import retained_activities
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import EXECUTION_COVERAGE_CONFLICT_REASON_CODE
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _broker_order_fixture
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _deploy, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_manual_chain_foreign_member_fee_coverage import (
    _account_execution_coverage,
    _bot_entry,
    _bot_entry_filled_over_rest,
    _read_account_activity,
    _reading_after_every_execution,
    _recovery_actions,
)
from tests.broker.alpaca.clerk.sqlite.test_manual_order_filled_over_rest import (
    _EXEC_1,
    _EXEC_2,
    _ActivityFeed,
    _coverage_conflict_episodes,
    _credited,
    _fill_frame,
    _sweep,
)
from tests.broker.alpaca.clerk.sqlite.test_manual_order_replaced_at_alpaca import _Website


def _another_bots_entry_is_admitted(repo: ClerkSqliteRepository) -> bool:
    return accept_enter(
        repo, account_id=repo.account_id, strategy_instance_id="b", decision_id="after-disagreement",
        lifecycle_run_id="run-b", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        reference_price=100, envelope=_reading_after_every_execution(repo),
    ).created


def _fills(repo: ClerkSqliteRepository, order_ref: str) -> list[tuple[str | None, str, float, float]]:
    """Every effective fill of the order, exact or cumulative: its execution id, evidence source, shares and price."""
    return sorted(
        ((fill["execution_id"], fill["evidence_source"], fill["qty"], fill["price"]) for fill in repo.fills_for_order(order_ref)),
        key=lambda fill: (fill[0] or "", fill[1]),
    )


def _conflicts_naming(repo: ClerkSqliteRepository) -> list[tuple[str | None, str]]:
    """Each coverage conflict's resolution state and the execution it names."""
    rows = repo._conn.execute(
        "SELECT resolved_at_ms, facts_json FROM uncertainties WHERE reason_code = ? ORDER BY observed_at_ms",
        (EXECUTION_COVERAGE_CONFLICT_REASON_CODE,),
    ).fetchall()
    return [
        ("active" if row["resolved_at_ms"] is None else "resolved", row["facts_json"].split('"execution_id":"')[1].split('"')[0])
        for row in rows
    ]


async def test_executions_whose_average_rest_rounded_to_the_cent_replace_the_bots_rest_total(tmp_path: Path) -> None:
    """REST reports the bot's 5 shares at an average of 100.00; they executed as 3 at 100.00 and 2 at 100.01.

    Their average, 100.004, is under a cent from REST's: the same executions,
    rounded. The fee-evidence read replaces the cumulative with them, the
    sweep keeps it so, fee and P&L coverage are complete, and the other
    bot's entry is admitted.
    """
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo, "a", 60_000)
        _deploy(repo, "b", 30_000)
        order_ref, filled = _bot_entry_filled_over_rest(repo, decision_id="rounded-average")
        feed = _ActivityFeed()
        feed.fill(execution_id=_EXEC_1, order_id=filled.order_id, quantity=3, price=100.00, at_ms=NOON)
        feed.fill(execution_id=_EXEC_2, order_id=filled.order_id, quantity=2, price=100.01, at_ms=NOON + 1)

        await _read_account_activity(repo, feed)
        await _sweep(repo, _Website(repo=repo), feed, spy_held=5.0)

        assert _credited(repo, order_ref) == [
            (_EXEC_1, "activity_recovery", 3.0, 100.0),
            (_EXEC_2, "activity_recovery", 2.0, 100.01),
        ]
        assert "active" not in _coverage_conflict_episodes(repo)
        assert repo.attributed_positions_by_symbol() == pytest.approx({"SPY": 5.0}, abs=1e-9, rel=0)
        fees = repo.fee_attribution(now_ms=repo.clock())
        assert fees.known, fees.unresolved
        assert _account_execution_coverage(repo) == "complete"
        assert _another_bots_entry_is_admitted(repo)
    finally:
        repo.close()


async def test_a_bot_execution_priced_two_cents_from_rest_keeps_the_rest_total_and_admits_entries(
    tmp_path: Path,
) -> None:
    """Alpaca's fill history prices the bot's 5 shares at 100.02; REST's average is 100.00.

    Two cents is a real disagreement, so the read quarantines the execution
    behind a coverage conflict, and the sweep's order-total proof closes it
    (#2346): the broker's final total is the 5 shares recorded. The REST
    total stays the order's fill, but the quarantined execution names every
    share of it, so fee coverage is complete and the other bot's entry is
    admitted. The account's P&L coverage still reads incomplete -- whose
    price stands is not settled -- so the bot asks for attention.
    """
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo, "a", 60_000)
        _deploy(repo, "b", 30_000)
        order_ref, filled = _bot_entry_filled_over_rest(repo, decision_id="two-cents-off")
        feed = _ActivityFeed()
        feed.fill(execution_id=_EXEC_1, order_id=filled.order_id, quantity=5, price=100.02, at_ms=NOON)

        await _read_account_activity(repo, feed)
        assert _coverage_conflict_episodes(repo) == ["active"]
        await _sweep(repo, _Website(repo=repo), feed, spy_held=5.0)
        await _read_account_activity(repo, feed)

        assert _coverage_conflict_episodes(repo) == ["resolved"]
        assert _credited(repo, order_ref) == [(None, "cumulative_recovery", 5.0, 100.0)]
        fees = repo.fee_attribution(now_ms=repo.clock())
        assert fees.known, fees.unresolved
        assert _account_execution_coverage(repo) == "incomplete"
        assert _another_bots_entry_is_admitted(repo)
    finally:
        repo.close()


async def test_an_activity_contradicting_a_recorded_bot_execution_raises_one_coverage_conflict(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The stream recorded EXEC_1 as 2 of the bot's 4 shares; REST credited the other 2 as a cumulative.

    Alpaca's fill history then says EXEC_1 was 3 shares and EXEC_2 1. That
    contradicts what the order recorded, so the read raises one coverage
    conflict naming EXEC_1; the order then leaves the recovery, and later
    reads and sweeps add nothing.
    """
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo)
        accepted = _bot_entry(repo, decision_id="contradicted", quantity=4)
        assert accepted.order_ref is not None and accepted.effect_operation_id is not None
        order_ref = accepted.order_ref
        partial = _broker_order_fixture(
            order_ref, status="partially_filled", quantity=4, filled_quantity=2, filled_avg_price=100,
        ).model_copy(update={"updated_at_ms": NOON, "observed_at_ms": NOON})
        assert await _fill_frame(repo, partial, execution_id=_EXEC_1, quantity=2, price=100) == "order_event"
        filled = partial.model_copy(update={"status": "filled", "filled_quantity": 4, "filled_at_ms": NOON})
        fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=filled)
        assert _fills(repo, order_ref) == [(None, "cumulative_recovery", 2.0, 100.0), (_EXEC_1, "websocket", 2.0, 100.0)]
        feed = _ActivityFeed()
        feed.fill(execution_id=_EXEC_1, order_id=filled.order_id, quantity=3, price=100, at_ms=NOON)
        feed.fill(execution_id=_EXEC_2, order_id=filled.order_id, quantity=1, price=100, at_ms=NOON + 1)
        caplog.set_level(logging.INFO)

        await _read_account_activity(repo, feed)
        transitions = len(repo.transitions_for_order(order_ref))
        await _sweep(repo, _Website(repo=repo), feed, spy_held=4.0)
        await _read_account_activity(repo, feed)

        assert _conflicts_naming(repo) == [("active", _EXEC_1)]
        assert _recovery_actions(caplog, "bot_order_execution_contradicted") == [order_ref]
        assert len(repo.transitions_for_order(order_ref)) == transitions
        assert _fills(repo, order_ref) == [(None, "cumulative_recovery", 2.0, 100.0), (_EXEC_1, "websocket", 2.0, 100.0)]
    finally:
        repo.close()


async def test_executions_a_bot_order_already_accounts_for_past_its_quantity_are_reported_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The bot's 5 shares were recovered from account activity; recovery then reads them against a 4-share cap.

    Nothing new would be credited, so nothing is refused or raised, and the
    excess is reported once however many passes read the same evidence.
    """
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo)
        order_ref, filled = _bot_entry_filled_over_rest(repo, decision_id="past-quantity")
        feed = _ActivityFeed()
        feed.fill(execution_id=_EXEC_1, order_id=filled.order_id, quantity=5, price=100, at_ms=NOON)
        await _read_account_activity(repo, feed)
        assert _credited(repo, order_ref) == [(_EXEC_1, "activity_recovery", 5.0, 100.0)]
        order = repo.order(order_ref)
        assert order is not None
        owner = repo.effect_operation(order.effect_operation_id)
        assert owner is not None
        caplog.set_level(logging.INFO)

        outcomes = [
            record_activity_executions(
                repo, subject=BOT_ORDER, order_ref=order_ref, owner=owner,
                leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=4), broker_order_id=filled.order_id,
                member_ids={filled.order_id}, quantity_cap=4, require_total=True,
                fills=retained_activities(repo._conn).unique.values(),
            )
            for _ in range(3)
        ]

        assert {(outcome.over_quantity, outcome.grew) for outcome in outcomes} == {(True, False)}
        assert _recovery_actions(caplog, "bot_order_recovered_executions_exceed_order") == [order_ref]
        assert _coverage_conflict_episodes(repo) == []
    finally:
        repo.close()


@pytest.mark.parametrize(
    ("retained", "cumulative", "explained"),
    [
        ((5.0,), (5.0,), True),
        ((3.0, 2.0), (5.0,), True),
        ((2.0,), (2.0, 3.0), False),
        ((5.0 + 2e-9,), (5.0,), False),
        ((5.0 + 5e-10,), (5.0,), True),
        ((), (5.0,), False),
        ((5.0,), (), False),
        ((math.nan,), (5.0,), False),
        ((5.0, 0.0), (5.0,), False),
    ],
)
def test_order_total_retained_exacts_explain_a_cumulative_only_by_every_share_of_it(
    retained: tuple[float, ...], cumulative: tuple[float, ...], explained: bool,
) -> None:
    assert (
        order_total_retained_exacts_explain_cumulative(retained_quantities=retained, cumulative_quantities=cumulative)
        is explained
    )
