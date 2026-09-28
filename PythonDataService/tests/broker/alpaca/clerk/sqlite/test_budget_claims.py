"""Conservation at execution/observation boundaries, including older custody."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.budget_projection import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.manual_orders import accept_manual_order
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_acknowledgement
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerActivity, BrokerOrder, BrokerOrderLeg
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
    fees = repo.fee_attribution(now_ms=NOON)
    assert not fees.known
    with pytest.raises(BudgetUnavailable, match="fill coverage"):
        repo.account_budget(cash=1000, seen_before_ms=NOON + 1)
    _append_slice(repo, accepted, execution_id="late-legacy-fill",
                  quantity=float(Decimal(str(reported)) - Decimal(str(recorded))), source_event_at_ms=NOON, fee=0)
    assert repo.fee_attribution(now_ms=NOON).known
    assert repo.account_budget(cash=1000, seen_before_ms=NOON + 1).available == 1000


def test_canceled_order_without_fills_does_not_invent_missing_population(day_pnl_repo) -> None:
    repo = day_pnl_repo
    accepted = _accept_day_pnl_enter(repo, decision_id="canceled-zero")
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    fold_order_acknowledgement(repo, effect_operation_id=accepted.effect_operation_id,
        order=filled_order(accepted.order_ref).model_copy(update={"status": "canceled", "filled_quantity": 0}))
    assert repo.fee_attribution(now_ms=NOON).known


def test_first_tiny_broker_fill_is_retained_instead_of_becoming_zero(day_pnl_repo) -> None:
    repo = day_pnl_repo
    accepted = _accept_day_pnl_enter(repo, decision_id="tiny-total")
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    order = filled_order(accepted.order_ref).model_copy(update={"status": "canceled", "filled_quantity": 1e-10})
    fold_order_acknowledgement(repo, effect_operation_id=accepted.effect_operation_id, order=order)
    assert repo.latest_reported_filled_quantity(accepted.order_ref) == 1e-10
    assert not repo.fee_attribution(now_ms=NOON).known
    _append_slice(repo, accepted, execution_id="tiny-exact", quantity=1e-10, source_event_at_ms=NOON, fee=0)
    assert repo.fee_attribution(now_ms=NOON).known


@pytest.mark.parametrize("prior,current", [(1, 1.0000000001), (1.0000000001, 1)])
def test_same_state_broker_quantity_changes_are_retained_exactly(day_pnl_repo, prior: float, current: float) -> None:
    repo = day_pnl_repo
    accepted = _accept_day_pnl_enter(repo, decision_id="tiny-change")
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    _append_slice(repo, accepted, execution_id="exact-one", quantity=1, source_event_at_ms=NOON, fee=0)
    order = filled_order(accepted.order_ref).model_copy(update={"filled_quantity": prior})
    fold_order_acknowledgement(repo, effect_operation_id=accepted.effect_operation_id, order=order)
    restated = order.model_copy(update={"filled_quantity": current})
    fold_order_acknowledgement(repo, effect_operation_id=accepted.effect_operation_id, order=restated)
    assert repo.latest_reported_filled_quantity(accepted.order_ref) == current
    assert repo.fee_attribution(now_ms=NOON).known == (current == 1)
    after = repo.custody_transitions()
    fold_order_acknowledgement(repo, effect_operation_id=accepted.effect_operation_id, order=restated)
    assert repo.custody_transitions() == after


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


def _external_order(*, state: str = "accepted", filled: float = 0, observed_at: int = NOON - 86_400_000) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca", order_id="external-budget-order", client_order_id="console-order",
        symbol="SPY", asset_class="us_equity", side="buy", order_type="limit", time_in_force="gtc",
        quantity=6, filled_quantity=filled, limit_price=100, stop_price=None,
        filled_avg_price=100 if filled else None, status=state, submitted_at_ms=observed_at,
        created_at_ms=observed_at, updated_at_ms=observed_at, filled_at_ms=None,
        canceled_at_ms=None, expired_at_ms=None, observed_at_ms=observed_at,
    )


def _external_fill(*, quantity: float = 6, observed_at: int = NOON) -> BrokerActivity:
    return BrokerActivity(
        broker="alpaca", activity_id="external-budget-fill", native_order_id="external-budget-order",
        activity_type="FILL", category="trade_activity", symbol="SPY", side="buy",
        quantity=quantity, price=100, net_amount=None, occurred_at_ms=NOON, observed_at_ms=observed_at,
    )


def test_reviewing_prior_day_external_gtc_order_does_not_release_its_money(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.sqlite.external_orders import acknowledge_external_order, observe_external_order

    repo = _new_budget_repo(tmp_path)
    try:
        external = observe_external_order(repo, order=_external_order())
        acknowledge_external_order(repo, external_order_id=external.external_order_id, operator="owner")
        with pytest.raises(BudgetUnavailable, match="external order"):
            _deploy(repo)
        observe_external_order(repo, order=_external_order(state="canceled", observed_at=NOON))
        assert repo.account_budget(cash=1000, seen_before_ms=NOON).available == 1000
    finally:
        repo.close()


def test_external_fill_before_order_observation_keeps_its_unseen_debit(tmp_path: Path) -> None:
    repo = _new_budget_repo(tmp_path)
    try:
        record_fee_evidence(repo, [_external_fill()], checked_at_ms=NOON, history_complete=True)
        unseen = repo.account_budget(cash=1000, seen_before_ms=NOON)
        assert unseen.order_claims == 600
        assert unseen.fee_claims == Decimal("0.01")
        assert unseen.available == Decimal("399.99")
        # Duplicate polling cannot move an already recognized debit's cutoff.
        repo.clock.advance(1)
        record_fee_evidence(repo, [_external_fill(observed_at=NOON + 1)], checked_at_ms=NOON + 1, history_complete=True)
        seen = repo.account_budget(cash=400, seen_before_ms=NOON + 1)
        assert seen.order_claims == 0 and seen.available == unseen.available
        assert repo.account_budget(cash=1000, seen_before_ms=NOON) == unseen
    finally:
        repo.close()


def test_terminal_external_order_needs_all_its_exact_executions_then_replays(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.sqlite.external_orders import acknowledge_external_order, observe_external_order

    repo = _new_budget_repo(tmp_path)
    external = observe_external_order(repo, order=_external_order(state="canceled", filled=3))
    acknowledge_external_order(repo, external_order_id=external.external_order_id, operator="owner")
    record_fee_evidence(repo, [_external_fill(quantity=2)], checked_at_ms=NOON, history_complete=True)
    with pytest.raises(BudgetUnavailable, match="external order"):
        repo.account_budget(cash=1000, seen_before_ms=NOON)
    # The exact missing execution is a separate activity, never a made-up cumulative fill.
    remainder = _external_fill(quantity=1).model_copy(update={"activity_id": "external-remainder"})
    record_fee_evidence(repo, [_external_fill(quantity=2), remainder], checked_at_ms=NOON, history_complete=True)
    expected = repo.account_budget(cash=1000, seen_before_ms=NOON)
    assert expected.order_claims == 300
    database = repo.db_path
    repo.close()
    database.rename(database.with_suffix(".saved"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=lambda: NOON)
    try:
        # Freshness is this process's producer read, never replayed: the
        # rebuilt authority refuses until it reads again, which adds nothing.
        with pytest.raises(BudgetUnavailable, match="missing or stale"):
            rebuilt.account_budget(cash=1000, seen_before_ms=NOON)
        assert not record_fee_evidence(rebuilt, [_external_fill(quantity=2), remainder], checked_at_ms=NOON, history_complete=True)
        assert rebuilt.account_budget(cash=1000, seen_before_ms=NOON) == expected
    finally:
        rebuilt.close()


def test_legacy_external_observation_stays_hash_compatible_and_unknown_until_refreshed(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.sqlite.external_orders import acknowledge_external_order, observe_external_order
    from app.broker.alpaca.clerk.sqlite.facts import ExternalOrderObservedFacts
    from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize

    repo = _new_budget_repo(tmp_path)
    try:
        legacy = canonicalize({
            "external_order_id": "external-budget-order", "broker_order_id": "external-budget-order",
            "client_order_id": "console-order", "symbol": "SPY", "side": "BUY", "qty": 6.0,
            "order_type": "limit", "limit_price": 100.0, "stop_price": None, "filled_avg_price": None,
            "observed_at_ms": NOON - 86_400_000, "evidence_refs": ["external-budget-order"],
        })
        assert ExternalOrderObservedFacts.from_facts_json(legacy).to_facts_json() == legacy
        repo.append_transition(TransitionInput(
            broker_order_id="external-budget-order", transition_kind="EXTERNAL_ORDER_OBSERVED",
            custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK", operation_state="succeeded",
            clerk_observed_at_ms=NOON, summary_code="EXTERNAL_ORDER_OBSERVED", facts_json=legacy,
        ))
        acknowledge_external_order(repo, external_order_id="external-budget-order", operator="owner")
        history = tuple(repo.custody_transitions())
        with pytest.raises(BudgetUnavailable, match="unknown cash obligation"):
            repo.account_budget(cash=1000, seen_before_ms=NOON)
        # Same original fields, now with explicit cancellation proof: not a duplicate.
        observe_external_order(repo, order=_external_order(state="canceled"))
        assert repo.account_budget(cash=1000, seen_before_ms=NOON).available == 1000
        assert tuple(repo.custody_transitions())[:len(history)] == history
    finally:
        repo.close()


@pytest.mark.parametrize("recorded", [2.9999999999, 3.0000000001])
def test_external_fill_coverage_has_no_quantity_epsilon(tmp_path: Path, recorded: float) -> None:
    from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order

    repo = _new_budget_repo(tmp_path)
    try:
        observe_external_order(repo, order=_external_order(state="canceled", filled=3))
        record_fee_evidence(repo, [_external_fill(quantity=recorded)], checked_at_ms=NOON, history_complete=True)
        with pytest.raises(BudgetUnavailable, match="complete execution population"):
            repo.account_budget(cash=1000, seen_before_ms=NOON)
    finally:
        repo.close()


@pytest.mark.parametrize("filled", [0, 3])
async def test_reconcile_refreshes_old_reviewed_gtc_cancellation_and_retains_exact_debit(tmp_path: Path, filled: int) -> None:
    from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
    from app.broker.alpaca.clerk.sqlite.external_orders import acknowledge_external_order, observe_external_order
    from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
    from tests.broker.alpaca.clerk.sqlite.test_reconcile import _FakeRead, _FakeTrade

    repo = _new_budget_repo(tmp_path)
    try:
        external = observe_external_order(repo, order=_external_order())
        acknowledge_external_order(repo, external_order_id=external.external_order_id, operator="owner")
        with pytest.raises(BudgetUnavailable, match="external order"):
            repo.account_budget(cash=1000, seen_before_ms=NOON)
        terminal = _external_order(state="canceled", filled=filled, observed_at=NOON)
        trade = _FakeTrade(lookup_result=terminal)
        # This GTC was submitted yesterday. It is absent from today's open
        # snapshot; exact identity lookup must supply its cancellation proof.
        await reconcile_account(repo, read=_FakeRead(), trade=trade, pricing=UNPRICEABLE_RECOVERY,
            trigger="OPERATOR_RECONCILE_NOW")
        assert trade.lookup_calls == ["console-order"]
        assert repo.external_order(external.external_order_id).broker_state == "canceled"
        if filled:
            with pytest.raises(BudgetUnavailable, match="Reconcile account executions"):
                repo.account_budget(cash=1000, seen_before_ms=NOON)
            record_fee_evidence(repo, [_external_fill(quantity=filled)], checked_at_ms=NOON, history_complete=True)
        budget = repo.account_budget(cash=1000, seen_before_ms=NOON)
        assert budget.order_claims == Decimal(filled * 100)
        # Confirmed terminal evidence is retained; a second pass needs no
        # lookup and cannot forget the debit before a newer cash observation.
        await reconcile_account(repo, read=_FakeRead(), trade=trade, pricing=UNPRICEABLE_RECOVERY)
        assert trade.lookup_calls == ["console-order"]
        assert repo.account_budget(cash=1000, seen_before_ms=NOON) == budget
    finally:
        repo.close()


@pytest.mark.parametrize("answer", ["absent", "unavailable", "wrong_identity"])
async def test_external_refresh_requires_positive_matching_order_proof(tmp_path: Path, answer: str) -> None:
    from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
    from app.broker.alpaca.clerk.sqlite.external_orders import acknowledge_external_order, observe_external_order
    from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
    from app.broker.contract.errors import BrokerUnavailable
    from tests.broker.alpaca.clerk.sqlite.test_reconcile import _FakeRead, _FakeTrade

    repo = _new_budget_repo(tmp_path)
    try:
        external = observe_external_order(repo, order=_external_order())
        acknowledge_external_order(repo, external_order_id=external.external_order_id, operator="owner")
        trade = _FakeTrade(lookup_absent=answer == "absent",
            lookup_error=BrokerUnavailable("offline") if answer == "unavailable" else None,
            lookup_result=_external_order(state="canceled").model_copy(update={"order_id": "other"}))
        await reconcile_account(repo, read=_FakeRead(), trade=trade, pricing=UNPRICEABLE_RECOVERY)
        assert trade.lookup_calls == ["console-order"]
        assert repo.external_order(external.external_order_id).broker_state == "accepted"
        with pytest.raises(BudgetUnavailable, match="external order"):
            repo.account_budget(cash=1000, seen_before_ms=NOON)
        retry = _FakeTrade(lookup_result=_external_order(state="canceled", observed_at=NOON))
        await reconcile_account(repo, read=_FakeRead(), trade=retry, pricing=UNPRICEABLE_RECOVERY)
        assert repo.account_budget(cash=1000, seen_before_ms=NOON).available == 1000
    finally:
        repo.close()


async def test_external_lookup_cannot_overwrite_a_fill_arriving_during_its_read(tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
    from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
    from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
    from tests.broker.alpaca.clerk.sqlite.test_reconcile import _FakeRead, _FakeTrade

    repo = _new_budget_repo(tmp_path)

    class FillDuringLookup(_FakeTrade):
        async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
            observe_external_order(repo, order=_external_order(state="filled", filled=6, observed_at=NOON))
            return await super().get_order_by_client_order_id(client_order_id)

    try:
        external = observe_external_order(repo, order=_external_order())
        trade = FillDuringLookup(lookup_result=_external_order(state="canceled", observed_at=NOON))
        await reconcile_account(repo, read=_FakeRead(), trade=trade, pricing=UNPRICEABLE_RECOVERY)
        assert repo.external_order(external.external_order_id).broker_state == "filled"
        record_fee_evidence(repo, [_external_fill()], checked_at_ms=NOON, history_complete=True)
        assert repo.account_budget(cash=1000, seen_before_ms=NOON).order_claims == 600
    finally:
        repo.close()
