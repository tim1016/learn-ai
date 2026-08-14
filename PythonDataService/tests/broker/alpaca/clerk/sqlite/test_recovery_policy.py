"""Typed recovery catalog and action-token policy tests (#1395)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.broker.alpaca.clerk.sqlite.projection_models import (
    ProjectedExecutionCoverageConflict,
    ProjectedOrder,
    ProjectedPosition,
    ProjectedReconciliation,
    ProjectedRun,
    ProjectedUncertainty,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    AuthorityRecoveryProof,
    RecoveryActionUnavailableError,
    RecoveryPolicyContext,
    StaleRecoveryTokenError,
    build_projection_guidance,
    build_recovery_catalog,
    recheck_recovery_action,
)
from app.schemas.alpaca_clerk_sqlite import RecoveryCapabilityResponse


def _context(**overrides) -> RecoveryPolicyContext:
    values = {
        "account_id": "PA-TEST",
        "strategy_instance_id": "spy-bot",
        "authority_generation": 1,
        "db_identity_token": "db-token",
        "authority_health": "healthy",
        "authority_health_reason": None,
        "control_revision": 7,
        "now_ms": 1_700_000_010_000,
        "runs": (
            ProjectedRun(
                run_id="run-1",
                strategy_instance_id="spy-bot",
                lifecycle_run_id="lifecycle-1",
                state="ACTIVE",
                started_at_ms=1_700_000_000_000,
                stopped_at_ms=None,
            ),
        ),
        "current_orders": (),
        "positions": (),
        "uncertainties": (),
        "latest_account_reconciliation": None,
    }
    values.update(overrides)
    return RecoveryPolicyContext(**values)


def test_healthy_catalog_omits_failure_and_generic_recovery_actions() -> None:
    actions = {action.action_id for action in build_recovery_catalog(_context())}

    assert actions == {
        "reconcile_now",
        "recover_exact_execution_evidence",
        "resolve_execution_coverage",
        "cancel_verified_working_orders",
        "prepare_safe_flatten",
        "stop_bot_decisions",
        "open_custody_timeline",
    }
    assert "clear_hold" not in actions
    assert "retry" not in actions
    assert "flatten" not in actions
    assert "rebuild_from_mirror" not in actions
    assert "reset_authority" not in actions


def test_coverage_resolution_requires_one_exact_economic_replacement() -> None:
    conflict = ProjectedExecutionCoverageConflict(
        uncertainty_id="uncertainty:coverage-1",
        order_ref="alpaca/intent-1",
        execution_id="execution-1",
        cumulative_fill_id="alpaca/intent-1:5.000000000",
        exact_qty=5.0,
        exact_price=100.0,
        exact_side="BUY",
        cumulative_qty=5.0,
        cumulative_price=100.0,
        cumulative_side="BUY",
        proof_available=True,
        unavailable_reason=None,
    )
    action = next(
        item
        for item in build_recovery_catalog(
            _context(execution_coverage_conflicts=(conflict,))
        )
        if item.action_id == "resolve_execution_coverage"
    )

    assert action.available is True
    assert action.execution_ref == conflict.uncertainty_id
    assert action.confirmation is not None
    assert action.evidence[1].reference == "execution:execution-1"
    with pytest.raises(StaleRecoveryTokenError):
        recheck_recovery_action(
            _context(
                control_revision=8,
                execution_coverage_conflicts=(conflict,),
            ),
            action_id=action.action_id,
            concurrency_token=action.concurrency_token,
        )


def test_coverage_resolution_explains_when_exact_proof_is_insufficient() -> None:
    conflict = ProjectedExecutionCoverageConflict(
        uncertainty_id="uncertainty:coverage-1",
        order_ref="alpaca/intent-1",
        execution_id="execution-1",
        cumulative_fill_id="alpaca/intent-1:5.000000000",
        exact_qty=5.0,
        exact_price=101.0,
        exact_side="BUY",
        cumulative_qty=5.0,
        cumulative_price=100.0,
        cumulative_side="BUY",
        proof_available=False,
        unavailable_reason="The exact execution and cumulative recovery economics differ.",
    )
    action = next(
        item
        for item in build_recovery_catalog(
            _context(execution_coverage_conflicts=(conflict,))
        )
        if item.action_id == "resolve_execution_coverage"
    )

    assert action.available is False
    assert action.unavailable_reason_code == "COVERAGE_PROOF_INSUFFICIENT"
    assert action.next_step == (
        "Keep the exposure blocked and obtain exact broker execution coverage; no generic override is available."
    )


def test_historical_exact_evidence_recovery_requires_one_bot_scoped_missing_exact() -> None:
    conflict = ProjectedExecutionCoverageConflict(
        uncertainty_id="uncertainty:historical-1",
        order_ref="alpaca/intent-1",
        execution_id="execution-1",
        cumulative_fill_id="alpaca/intent-1:5.000000000",
        exact_qty=None,
        exact_price=None,
        exact_side=None,
        cumulative_qty=5.0,
        cumulative_price=100.0,
        cumulative_side="BUY",
        proof_available=False,
        unavailable_reason="The exact execution quarantine is absent; fresh exact evidence is required.",
    )
    bot_action = next(
        action
        for action in build_recovery_catalog(
            _context(execution_coverage_conflicts=(conflict,))
        )
        if action.action_id == "recover_exact_execution_evidence"
    )
    account_action = next(
        action
        for action in build_recovery_catalog(
            _context(
                strategy_instance_id=None,
                execution_coverage_conflicts=(conflict,),
            )
        )
        if action.action_id == "recover_exact_execution_evidence"
    )

    assert bot_action.available is True
    assert bot_action.primary is True
    assert bot_action.execution_ref == conflict.uncertainty_id
    assert account_action.available is False
    assert account_action.unavailable_reason_code == "BOT_SCOPE_REQUIRED"


def test_historical_exact_evidence_recovery_identifies_every_bot_conflict() -> None:
    conflicts = (
        ProjectedExecutionCoverageConflict(
            uncertainty_id="uncertainty:historical-1",
            order_ref="alpaca/intent-1",
            execution_id="execution-1",
            cumulative_fill_id="alpaca/intent-1:5.000000000",
            exact_qty=None,
            exact_price=None,
            exact_side=None,
            cumulative_qty=5.0,
            cumulative_price=100.0,
            cumulative_side="BUY",
            proof_available=False,
            unavailable_reason="Exact evidence is missing.",
        ),
        ProjectedExecutionCoverageConflict(
            uncertainty_id="uncertainty:historical-2",
            order_ref="alpaca/intent-2",
            execution_id="execution-2",
            cumulative_fill_id="alpaca/intent-2:2.000000000",
            exact_qty=None,
            exact_price=None,
            exact_side=None,
            cumulative_qty=2.0,
            cumulative_price=200.0,
            cumulative_side="BUY",
            proof_available=False,
            unavailable_reason="Exact evidence is missing.",
        ),
    )
    action = next(
        item
        for item in build_recovery_catalog(_context(execution_coverage_conflicts=conflicts))
        if item.action_id == "recover_exact_execution_evidence"
    )

    assert action.available is False
    assert action.unavailable_reason_code == "COVERAGE_RECOVERY_SELECTION_REQUIRED"
    assert [evidence.reference for evidence in action.evidence] == [
        "order:alpaca/intent-1",
        "order:alpaca/intent-2",
    ]


def test_failure_catalog_requires_exact_rebuild_and_reset_prerequisites() -> None:
    failed = _context(
        authority_health="failed",
        authority_health_reason="integrity check failed",
        recovery_proof=AuthorityRecoveryProof(),
    )
    blocked = {action.action_id: action for action in build_recovery_catalog(failed)}

    assert set(blocked) == {
        "open_custody_timeline",
        "rebuild_from_mirror",
        "reset_authority",
    }
    assert blocked["rebuild_from_mirror"].available is False
    assert blocked["rebuild_from_mirror"].unavailable_reason_code == (
        "VERIFIED_MIRROR_REQUIRED"
    )
    assert blocked["reset_authority"].available is False
    assert blocked["reset_authority"].unavailable_reason_code == (
        "FRESH_FLAT_ORDER_FREE_PROOF_REQUIRED"
    )

    proven = replace(
        failed,
        recovery_proof=AuthorityRecoveryProof(
            mirror_verified=True,
            mirror_reference="mirror:sequence:42",
            broker_observed_at_ms=failed.now_ms - 1_000,
            broker_account_flat=True,
            broker_order_free=True,
            broker_proof_reference="alpaca-proof:flat:42",
        ),
    )
    available = {
        action.action_id: action for action in build_recovery_catalog(proven)
    }
    assert available["rebuild_from_mirror"].available is True
    assert available["reset_authority"].available is True
    assert available["reset_authority"].confirmation is not None


def test_action_tokens_ignore_unrelated_revision_but_reject_relevant_change() -> None:
    original = _context()
    stop = next(
        action
        for action in build_recovery_catalog(original)
        if action.action_id == "stop_bot_decisions"
    )
    unrelated_revision = replace(original, control_revision=99)
    rechecked = recheck_recovery_action(
        unrelated_revision,
        action_id=stop.action_id,
        concurrency_token=stop.concurrency_token,
    )
    assert rechecked.available is True

    stopped_run = replace(
        original.runs[0],
        state="STOPPED",
        stopped_at_ms=original.now_ms,
    )
    with pytest.raises(StaleRecoveryTokenError):
        recheck_recovery_action(
            replace(original, runs=(stopped_run,)),
            action_id=stop.action_id,
            concurrency_token=stop.concurrency_token,
        )


def test_current_but_unavailable_action_is_a_typed_conflict() -> None:
    context = _context(runs=())
    stop = next(
        action
        for action in build_recovery_catalog(context)
        if action.action_id == "stop_bot_decisions"
    )

    with pytest.raises(RecoveryActionUnavailableError) as captured:
        recheck_recovery_action(
            context,
            action_id=stop.action_id,
            concurrency_token=stop.concurrency_token,
        )

    assert captured.value.capability.unavailable_reason_code == "NO_ACTIVE_BOT_RUN"


def test_safe_flatten_prepares_versioned_exact_close_plan() -> None:
    context = _context(
        strategy_instance_id=None,
        positions=(
            ProjectedPosition(
                strategy_instance_id="spy-bot",
                symbol="SPY",
                attributed_qty=1.25,
                updated_at_ms=1_700_000_008_000,
            ),
            ProjectedPosition(
                strategy_instance_id="qqq-bot",
                symbol="QQQ",
                attributed_qty=-2.5,
                updated_at_ms=1_700_000_009_000,
            ),
        ),
        latest_account_reconciliation=ProjectedReconciliation(
            reconciliation_id="reconciliation-17",
            effect_operation_id=None,
            order_ref=None,
            trigger="operator",
            attempted_at_ms=1_700_000_009_000,
            outcome="RESOLVED_SUCCESS",
            evidence_age_ms=1_000,
            evidence_refs=("alpaca:positions:17", "alpaca:orders:17"),
        ),
    )

    capability = next(
        action
        for action in build_recovery_catalog(context)
        if action.action_id == "prepare_safe_flatten"
    )

    assert capability.available is True
    assert capability.reduction_plan is not None
    assert capability.reduction_plan.version_token == capability.concurrency_token
    assert capability.reduction_plan.account_id == context.account_id
    assert capability.reduction_plan.authority_generation == context.authority_generation
    assert capability.reduction_plan.db_identity_token == context.db_identity_token
    assert capability.reduction_plan.control_revision == context.control_revision
    assert capability.reduction_plan.scope == "ACCOUNT_CLERK"
    assert capability.reduction_plan.strategy_instance_id is None
    assert capability.reduction_plan.reconciliation_id == "reconciliation-17"
    assert capability.reduction_plan.prepared_at_ms == context.now_ms
    assert capability.reduction_plan.expires_at_ms == 1_700_000_039_000
    assert tuple(
        (
            leg.strategy_instance_id,
            leg.symbol,
            leg.side,
            leg.quantity,
            leg.position_updated_at_ms,
        )
        for leg in capability.reduction_plan.legs
    ) == (
        (
            "qqq-bot",
            "QQQ",
            "buy",
            2.5,
            1_700_000_009_000,
        ),
        (
            "spy-bot",
            "SPY",
            "sell",
            1.25,
            1_700_000_008_000,
        ),
    )
    wire_plan = RecoveryCapabilityResponse.model_validate(
        capability
    ).model_dump(mode="json")["reduction_plan"]
    assert wire_plan is not None
    assert wire_plan["version_token"] == capability.concurrency_token
    assert wire_plan["legs"] == [
        {
            "strategy_instance_id": "qqq-bot",
            "symbol": "QQQ",
            "side": "buy",
            "quantity": 2.5,
            "position_updated_at_ms": 1_700_000_009_000,
        },
        {
            "strategy_instance_id": "spy-bot",
            "symbol": "SPY",
            "side": "sell",
            "quantity": 1.25,
            "position_updated_at_ms": 1_700_000_008_000,
        },
    ]


def test_safe_flatten_uses_canonical_sub_epsilon_flat_boundary() -> None:
    capability = next(
        action
        for action in build_recovery_catalog(
            _context(
                positions=(
                    ProjectedPosition(
                        strategy_instance_id="spy-bot",
                        symbol="SPY",
                        attributed_qty=0.0000000005,
                        updated_at_ms=1_700_000_008_000,
                    ),
                ),
            )
        )
        if action.action_id == "prepare_safe_flatten"
    )

    assert capability.available is False
    assert capability.unavailable_reason_code == "NO_ATTRIBUTED_EXPOSURE"
    assert capability.reduction_plan is None


def test_safe_flatten_plan_token_rejects_newer_position_evidence() -> None:
    position = ProjectedPosition(
        strategy_instance_id="spy-bot",
        symbol="SPY",
        attributed_qty=1.25,
        updated_at_ms=1_700_000_008_000,
    )
    context = _context(
        positions=(position,),
        latest_account_reconciliation=ProjectedReconciliation(
            reconciliation_id="reconciliation-17",
            effect_operation_id=None,
            order_ref=None,
            trigger="operator",
            attempted_at_ms=1_700_000_009_000,
            outcome="RESOLVED_SUCCESS",
            evidence_age_ms=1_000,
            evidence_refs=("alpaca:positions:17",),
        ),
    )
    capability = next(
        action
        for action in build_recovery_catalog(context)
        if action.action_id == "prepare_safe_flatten"
    )

    with pytest.raises(StaleRecoveryTokenError):
        recheck_recovery_action(
            replace(
                context,
                positions=(replace(position, updated_at_ms=position.updated_at_ms + 1),),
            ),
            action_id=capability.action_id,
            concurrency_token=capability.concurrency_token,
        )


def test_unavailable_safe_flatten_never_advertises_a_plan() -> None:
    capability = next(
        action
        for action in build_recovery_catalog(_context())
        if action.action_id == "prepare_safe_flatten"
    )

    assert capability.available is False
    assert capability.reduction_plan is None


@pytest.mark.parametrize("quantity", [float("inf"), float("-inf"), float("nan")])
def test_safe_flatten_fails_closed_for_non_finite_attributed_quantity(
    quantity: float,
) -> None:
    capability = next(
        action
        for action in build_recovery_catalog(
            _context(
                positions=(
                    ProjectedPosition(
                        strategy_instance_id="spy-bot",
                        symbol="SPY",
                        attributed_qty=quantity,
                        updated_at_ms=1_700_000_008_000,
                    ),
                ),
                latest_account_reconciliation=ProjectedReconciliation(
                    reconciliation_id="reconciliation-17",
                    effect_operation_id=None,
                    order_ref=None,
                    trigger="operator",
                    attempted_at_ms=1_700_000_009_000,
                    outcome="RESOLVED_SUCCESS",
                    evidence_age_ms=1_000,
                    evidence_refs=("alpaca:positions:17",),
                ),
            )
        )
        if action.action_id == "prepare_safe_flatten"
    )

    assert capability.available is False
    assert capability.unavailable_reason_code == "EXPOSURE_NOT_PROVEN"
    assert capability.reduction_plan is None


def test_account_safe_flatten_blocks_on_any_bot_uncertainty() -> None:
    reconciliation = ProjectedReconciliation(
        reconciliation_id="account-reconciliation-17",
        effect_operation_id=None,
        order_ref=None,
        trigger="OPERATOR_RECONCILE_NOW",
        attempted_at_ms=1_700_000_009_000,
        outcome="RESOLVED_SUCCESS",
        evidence_age_ms=1_000,
        evidence_refs=("fresh_account_snapshot",),
    )
    capability = next(
        action
        for action in build_recovery_catalog(
            _context(
                strategy_instance_id=None,
                positions=(
                    ProjectedPosition(
                        strategy_instance_id="spy-bot",
                        symbol="SPY",
                        attributed_qty=1.0,
                        updated_at_ms=1_700_000_008_000,
                    ),
                ),
                uncertainties=(
                    ProjectedUncertainty(
                        uncertainty_id="spy-uncertainty",
                        scope="CUSTODY_SUBJECT",
                        severity="warning",
                        blocks_new_exposure=True,
                        allows_reduction=False,
                        custody_owner="ACCOUNT_CLERK",
                        strategy_instance_id="spy-bot",
                        reason_code="ORDER_OUTCOME_UNKNOWN",
                        headline="SPY custody is uncertain",
                        explanation="The broker outcome is not proven.",
                        operator_impact="Account reduction is not proven.",
                        next_step="Reconcile the account.",
                        observed_at_ms=1_700_000_009_000,
                        evidence_age_ms=1_000,
                        evidence_refs=("order:spy",),
                    ),
                ),
                latest_account_reconciliation=reconciliation,
            )
        )
        if action.action_id == "prepare_safe_flatten"
    )

    assert capability.available is False
    assert capability.unavailable_reason_code == "EXPOSURE_NOT_PROVEN"
    assert capability.reduction_plan is None


def test_safe_flatten_requires_account_wide_reconciliation() -> None:
    effect_reconciliation = ProjectedReconciliation(
        reconciliation_id="effect-reconciliation-17",
        effect_operation_id="effect:spy-bot:decision-17",
        order_ref="order:spy-bot:decision-17",
        trigger="AUTOMATIC",
        attempted_at_ms=1_700_000_009_000,
        outcome="RESOLVED_SUCCESS",
        evidence_age_ms=1_000,
        evidence_refs=("order:spy-bot:decision-17",),
    )
    capability = next(
        action
        for action in build_recovery_catalog(
            _context(
                positions=(
                    ProjectedPosition(
                        strategy_instance_id="spy-bot",
                        symbol="SPY",
                        attributed_qty=1.0,
                        updated_at_ms=1_700_000_008_000,
                    ),
                ),
                latest_account_reconciliation=effect_reconciliation,
            )
        )
        if action.action_id == "prepare_safe_flatten"
    )

    assert capability.available is False
    assert capability.unavailable_reason_code == "CLEAN_RECONCILIATION_REQUIRED"
    assert capability.reduction_plan is None


def test_safe_flatten_rejects_position_observed_after_reconciliation() -> None:
    reconciliation = ProjectedReconciliation(
        reconciliation_id="account-reconciliation-17",
        effect_operation_id=None,
        order_ref=None,
        trigger="OPERATOR_RECONCILE_NOW",
        attempted_at_ms=1_700_000_008_000,
        outcome="RESOLVED_SUCCESS",
        evidence_age_ms=2_000,
        evidence_refs=("fresh_account_snapshot",),
    )
    capability = next(
        action
        for action in build_recovery_catalog(
            _context(
                positions=(
                    ProjectedPosition(
                        strategy_instance_id="spy-bot",
                        symbol="SPY",
                        attributed_qty=1.0,
                        updated_at_ms=1_700_000_009_000,
                    ),
                ),
                latest_account_reconciliation=reconciliation,
            )
        )
        if action.action_id == "prepare_safe_flatten"
    )

    assert capability.available is False
    assert capability.unavailable_reason_code == "POSITION_EVIDENCE_NOT_RECONCILED"
    assert capability.reduction_plan is None


def test_safe_flatten_checks_complete_current_orders_not_operation_page() -> None:
    reconciliation = ProjectedReconciliation(
        reconciliation_id="account-reconciliation-17",
        effect_operation_id=None,
        order_ref=None,
        trigger="OPERATOR_RECONCILE_NOW",
        attempted_at_ms=1_700_000_009_000,
        outcome="RESOLVED_SUCCESS",
        evidence_age_ms=1_000,
        evidence_refs=("fresh_account_snapshot",),
    )
    capability = next(
        action
        for action in build_recovery_catalog(
            _context(
                current_orders=(
                    ProjectedOrder(
                        order_ref="order:older-working",
                        client_order_id="order:older-working",
                        broker_order_id="alpaca-order-17",
                        role="ENTRY",
                        broker_state="accepted",
                        submitted_at_ms=1_700_000_007_000,
                        updated_at_ms=1_700_000_009_000,
                    ),
                ),
                positions=(
                    ProjectedPosition(
                        strategy_instance_id="spy-bot",
                        symbol="SPY",
                        attributed_qty=1.0,
                        updated_at_ms=1_700_000_008_000,
                    ),
                ),
                latest_account_reconciliation=reconciliation,
            )
        )
        if action.action_id == "prepare_safe_flatten"
    )

    assert capability.available is False
    assert capability.unavailable_reason_code == "WORKING_ORDERS_REQUIRE_CANCEL_FIRST"
    assert capability.reduction_plan is None


def test_bot_uncertainty_authors_scope_impact_and_next_step() -> None:
    uncertainty = ProjectedUncertainty(
        uncertainty_id="uncertain-1",
        scope="CUSTODY_SUBJECT",
        severity="warning",
        blocks_new_exposure=True,
        allows_reduction=False,
        custody_owner="ACCOUNT_CLERK",
        strategy_instance_id="spy-bot",
        reason_code="ORDER_OUTCOME_UNKNOWN",
        headline="SPY entries are paused",
        explanation="The last order outcome is not terminal yet.",
        operator_impact="Only this bot cannot create exposure.",
        next_step="The Clerk is reconciling automatically.",
        observed_at_ms=1_700_000_009_000,
        evidence_age_ms=1_000,
        evidence_refs=("order:1",),
    )

    guidance = build_projection_guidance(_context(uncertainties=(uncertainty,)))

    assert guidance.scope == "CUSTODY_SUBJECT"
    assert guidance.may_create_exposure is False
    assert guidance.impact == "Only this bot cannot create exposure."
    assert guidance.next_step == "The Clerk is reconciling automatically."
