"""Conservation at execution/observation boundaries, including older custody."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.budget_projection import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.fee_evidence import custody_fee_attribution, record_fee_evidence
from app.broker.alpaca.clerk.sqlite.manual_orders import accept_manual_order
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_acknowledgement
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _accept_day_pnl_enter
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _deploy, _gate, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_correction, _append_slice
from tests.broker.alpaca.clerk.sqlite.test_manual_orders import LEG_ID, OPERATOR_ID, TICKET_ID, filled_order


def _record_sale(repo: ClerkSqliteRepository, accepted: EnterSubmission, *, key: str, price: float, at_ms: int) -> None:
    """As in canonical FIFO tests, record economic fills on the owned order."""
    repo.append_transition(TransitionInput(
        strategy_instance_id=accepted.command.strategy_instance_id, run_id=accepted.command.run_id,
        command_id=accepted.command.command_id, effect_operation_id=accepted.effect_operation_id,
        order_ref=accepted.order_ref, transition_kind="EXECUTION_SLICE_FILLED",
        custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK", operation_state="in_progress",
        source_event_at_ms=at_ms, clerk_observed_at_ms=repo.clock(), summary_code="EXECUTION_SLICE_FILLED",
        facts_json=ExecutionSliceFilledFacts(execution_id=key, symbol="SPY", side="SELL", slice_qty=1,
            slice_price=price, fee=0, fee_fidelity="reported", evidence_source="websocket", source_event_at_ms=at_ms).to_facts_json(),
    ))


def test_completed_manual_buy_claims_its_unseen_exact_debit(tmp_path: Path) -> None:
    repo = _new_budget_repo(tmp_path)
    try:
        accepted = accept_manual_order(
            repo, account_id=repo.account_id, operator_id=OPERATOR_ID, ticket_id=TICKET_ID,
            leg_id=LEG_ID, leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=6),
        )
        leg = accepted.leg
        assert leg.order_ref and leg.effect_operation_id
        repo.append_transition(TransitionInput(
            command_id=accepted.command.command_id, effect_operation_id=leg.effect_operation_id,
            order_ref=leg.order_ref, transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress", source_event_at_ms=NOON, clerk_observed_at_ms=NOON,
            summary_code="EXECUTION_SLICE_FILLED", facts_json=ExecutionSliceFilledFacts(
                execution_id="manual-exact", symbol="SPY", side="BUY", slice_qty=6,
                slice_price=100, fee=.03, fee_fidelity="reported", evidence_source="websocket",
                source_event_at_ms=NOON,
            ).to_facts_json(),
        ))
        fold_order_acknowledgement(repo, effect_operation_id=leg.effect_operation_id, order=filled_order(leg.order_ref).model_copy(update={
            "quantity": 6, "filled_quantity": 6, "filled_avg_price": 100,
            "updated_at_ms": NOON, "observed_at_ms": NOON,
        }))
        assert repo.effect_operation(leg.effect_operation_id).state == "succeeded"
        unseen = repo.account_budget(cash=1000, seen_before_ms=NOON)
        assert unseen.order_claims == Decimal("600.03")
        assert unseen.fee_claims == 0  # Reported fee rides with its fill, exactly once.
        assert unseen.available == Decimal("399.97")
        seen = repo.account_budget(cash="399.97", seen_before_ms=NOON + 1)
        assert seen.order_claims == 0 and seen.available == unseen.available
    finally:
        repo.close()


@pytest.mark.parametrize("status,reported,recorded", [("filled", 10, 0), ("canceled", 3, 0), ("canceled", 3, 2.9999999999)])
def test_missing_legacy_terminal_execution_is_unknown_until_reconciled(
    day_pnl_repo, status: str, reported: float, recorded: float,
) -> None:
    repo = day_pnl_repo
    accepted = _accept_day_pnl_enter(repo, decision_id="legacy-without-reservation")
    assert accepted.order_ref and accepted.effect_operation_id
    submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id=accepted.command.strategy_instance_id,
                    lifecycle_run_id=accepted.command.run_id.split(":", 1)[1], clock=repo.clock)
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    if recorded:
        _append_slice(repo, accepted, execution_id="initial-legacy-fill", quantity=recorded, source_event_at_ms=NOON, fee=0)
    fold_order_acknowledgement(repo, effect_operation_id=accepted.effect_operation_id, order=filled_order(accepted.order_ref).model_copy(update={
        "status": status, "quantity": 10, "filled_quantity": reported, "filled_avg_price": 100,
        "updated_at_ms": NOON, "observed_at_ms": NOON,
    }))
    fees = custody_fee_attribution(repo._conn, now_ms=NOON)
    assert not fees.known
    with pytest.raises(BudgetUnavailable, match="fill coverage"):
        repo.account_budget(cash=1000, seen_before_ms=NOON + 1)
    _append_slice(repo, accepted, execution_id="late-legacy-fill",
                  quantity=float(Decimal(str(reported)) - Decimal(str(recorded))), source_event_at_ms=NOON, fee=0)
    assert custody_fee_attribution(repo._conn, now_ms=NOON).known
    assert repo.account_budget(cash=1000, seen_before_ms=NOON + 1).available == 1000


def test_canceled_order_without_fills_does_not_invent_missing_population(day_pnl_repo) -> None:
    repo = day_pnl_repo
    accepted = _accept_day_pnl_enter(repo, decision_id="canceled-zero")
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    fold_order_acknowledgement(repo, effect_operation_id=accepted.effect_operation_id,
        order=filled_order(accepted.order_ref).model_copy(update={"status": "canceled", "filled_quantity": 0}))
    assert custody_fee_attribution(repo._conn, now_ms=NOON).known


def test_partial_fill_prices_only_remaining_quantity_fee_provision(tmp_path: Path) -> None:
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo)
        accepted = accept_enter(
            repo, account_id=repo.account_id, strategy_instance_id="a", decision_id="partial",
            lifecycle_run_id="run-a", leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=4000),
            reference_price=.1, envelope=_gate(),
        )
        _append_slice(repo, accepted, execution_id="filled-part", quantity=3000, price=.1, source_event_at_ms=NOON)
        projection = repo.account_budget(cash=1000, seen_before_ms=NOON)
        own = projection.deployments[0]
        # CAT: 3000 filled shares settle to .01; the separate pending quote
        # for 1000 shares settles upward to .01. The original .02 quote may
        # not survive unchanged alongside the filled-share fee projection.
        assert own.fees == Decimal(".01")
        assert own.pending_orders == Decimal("100.01")
        assert own.free == Decimal("599.98")
        assert projection.available == 0
        _append_correction(repo, accepted, execution_id="corrected-part", superseded_execution_ref="filled-part",
                           quantity=2000, source_event_at_ms=NOON + 1)
        corrected = repo.account_budget(cash=1000, seen_before_ms=NOON)
        assert corrected.deployments[0].pending_orders == Decimal("200.01")
    finally:
        repo.close()


def test_interleaved_same_symbol_deployments_keep_own_fifo_after_correction_and_stop(tmp_path: Path) -> None:
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo, "a", 50_000)
        _deploy(repo, "b", 50_000)
        entries = {}
        for sid, price in (("a", 100), ("b", 200)):
            accepted = accept_enter(repo, account_id=repo.account_id, strategy_instance_id=sid,
                decision_id=f"buy-{sid}", lifecycle_run_id=f"run-{sid}",
                leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=2), reference_price=price, envelope=_gate())
            entries[sid] = accepted
        for sid, price in (("a", 100), ("b", 200)):
            accepted = entries[sid]
            _append_slice(repo, accepted, execution_id=f"buy-{sid}", quantity=2, price=price,
                          source_event_at_ms=NOON - (20 if sid == "a" else 10), fee=0)
        _record_sale(repo, entries["b"], key="sell-b", price=210, at_ms=NOON - 2)
        _record_sale(repo, entries["a"], key="sell-a", price=110, at_ms=NOON - 1)
        projection = repo.account_budget(cash=720, seen_before_ms=NOON + 1)
        by_sid = {row.strategy_instance_id: row for row in projection.deployments}
        assert by_sid["a"].realized_gross == by_sid["b"].realized_gross == 10
        assert by_sid["a"].free == 410 and by_sid["b"].free == 310
        assert projection.available == 0
        _append_correction(repo, entries["a"], execution_id="buy-a-correction", superseded_execution_ref="buy-a",
                           quantity=1.5, source_event_at_ms=NOON + 20)
        corrected = repo.account_budget(cash=770, seen_before_ms=NOON + 1)
        assert {row.strategy_instance_id: row.free for row in corrected.deployments} == {"a": 460, "b": 310}
        assert corrected.order_claims == 0 and corrected.available == 0
        submit_stop_run(repo, account_id=repo.account_id, strategy_instance_id="a", lifecycle_run_id="run-a", clock=repo.clock)
        stopped = repo.account_budget(cash=770, seen_before_ms=NOON + 1)
        assert stopped.available == 460
        assert {row.strategy_instance_id: row.position_cost for row in stopped.deployments} == {"a": 50, "b": 200}
    finally:
        repo.close()
