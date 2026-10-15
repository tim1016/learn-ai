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
    ProjectedUnknownExitOrder,
    RecoveryCapability,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    FRESH_EVIDENCE_MAX_AGE_MS,
    SWEEP_LIVENESS_BOUND_MS,
    UNCONDITIONAL_RECOVERY_ACTION_IDS,
    AuthorityRecoveryProof,
    PublishedPass,
    RecoveryActionUnavailableError,
    RecoveryPolicyContext,
    StaleRecoveryTokenError,
    build_recovery_catalog,
    recheck_recovery_action,
    unknown_order_lookup_settles_at_ms,
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


def test_unconditional_action_ids_really_are_available_with_nothing_wrong() -> None:
    """Pin the property ``UNCONDITIONAL_RECOVERY_ACTION_IDS`` asserts.

    The roster rail skips these on the strength of one claim: they are
    available without reading custody state, so their being ``primary`` proves
    nothing about the subject. A clean context -- no uncertainties, no
    positions, no conflicts -- is exactly the case where a *gated* cure must be
    unavailable, so anything still available here is undiagnostic by
    construction. If a future change gates one of these, this fails and the
    rail's exclusion must be revisited rather than silently over-hiding.
    """
    catalog = {action.action_id: action for action in build_recovery_catalog(_context())}

    for action_id in UNCONDITIONAL_RECOVERY_ACTION_IDS:
        assert catalog[action_id].available is True, action_id


def test_healthy_catalog_omits_failure_and_generic_recovery_actions() -> None:
    actions = {action.action_id for action in build_recovery_catalog(_context())}

    assert actions == {
        "reconcile_now",
        "recover_exact_execution_evidence",
        "resolve_execution_coverage",
        "cancel_verified_working_orders",
        "prepare_safe_flatten",
        "execute_safe_flatten",
        "discharge_attributed_residue",
        "end_expired_option",
        "settle_unknown_exit_order",
        "stop_bot_decisions",
        "open_custody_timeline",
    }
    assert "clear_hold" not in actions
    assert "retry" not in actions
    assert "flatten" not in actions
    assert "rebuild_from_mirror" not in actions
    assert "reset_authority" not in actions


def test_non_ascii_recovery_token_is_a_stale_token_refusal() -> None:
    with pytest.raises(StaleRecoveryTokenError):
        recheck_recovery_action(
            _context(), action_id="reconcile_now", concurrency_token="é" * 64
        )


@pytest.mark.parametrize(
    ("overrides", "says_cash_goes_back"),
    [({}, True), ({"account_id": "sim:spy-bot"}, False)],
)
def test_only_a_real_accounts_stop_says_unused_cash_goes_back(
    overrides: dict[str, str], says_cash_goes_back: bool
) -> None:
    """#2634: a Dry Run trades simulated cash, so nothing goes back to the account."""
    (stop,) = (
        action
        for action in build_recovery_catalog(_context(**overrides))
        if action.action_id == "stop_bot_decisions"
    )

    assert stop.confirmation is not None
    assert ("goes back to the account" in stop.confirmation.explanation) is says_cash_goes_back


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


def test_a_missing_fill_whose_exact_evidence_is_held_needs_no_recovery() -> None:
    """Codex on #2804: with the exact execution already quarantined, Resolve is the step, not Recover."""
    conflict = ProjectedExecutionCoverageConflict(
        uncertainty_id="uncertainty:coverage-1",
        order_ref="alpaca/intent-1",
        execution_id="execution-1",
        execution_ids=("execution-1",),
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
    actions = {
        action.action_id: action
        for action in build_recovery_catalog(_context(execution_coverage_conflicts=(conflict,)))
    }

    recover = actions["recover_exact_execution_evidence"]
    assert (recover.available, recover.unavailable_reason_code, recover.needed) == (
        False,
        "EXACT_EVIDENCE_ALREADY_AVAILABLE",
        False,
    )
    assert (actions["resolve_execution_coverage"].available, actions["resolve_execution_coverage"].needed) == (
        True,
        True,
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


def test_failure_catalog_offers_no_authority_recovery_action() -> None:
    """ADR 0047 (#1779): authority recovery is an offline ceremony.

    This replaces a test that asserted ``rebuild_from_mirror`` and
    ``reset_authority`` became available once a recovery proof was supplied.
    It passed only because it fabricated that proof directly: no production
    code path constructs one, and even with one, recovery refuses while the
    Clerk holds its execution lease -- and the lease holder is the sweep that
    would have had to produce the proof. The actions could never fire, so
    they are no longer presented.
    """
    failed = _context(
        authority_health="failed",
        authority_health_reason="integrity check failed",
        recovery_proof=AuthorityRecoveryProof(),
    )

    blocked = {action.action_id: action for action in build_recovery_catalog(failed)}

    assert set(blocked) == {"open_custody_timeline"}
    # The reader is honest about being unusable on a failed authority, and
    # points at the offline ceremony rather than at another button.
    assert blocked["open_custody_timeline"].available is False
    assert blocked["open_custody_timeline"].unavailable_reason_code == (
        "READABLE_TIMELINE_REQUIRED"
    )

    # A proof cannot resurrect them either -- the ids are gone from the
    # surface, not merely gated behind evidence.
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
    assert {action.action_id for action in build_recovery_catalog(proven)} == {
        "open_custody_timeline"
    }


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


def test_a_stop_with_no_active_run_says_where_a_half_done_stop_is_finished() -> None:
    """A STOP that committed while the bot's process kept running leaves no active run to offer a Stop for.

    The panel has no control for that: the refusal names the raw ``runs/stop`` route (#3217).
    """
    stop = next(
        action
        for action in build_recovery_catalog(_context(runs=()))
        if action.action_id == "stop_bot_decisions"
    )

    assert stop.available is False
    assert "/bots/{strategy_instance_id}/runs/stop" in stop.next_step


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


def _account_uncertainty(
    reason_code: str, *, observed_at_ms: int = 1_700_000_009_000
) -> ProjectedUncertainty:
    return ProjectedUncertainty(
        uncertainty_id=f"episode:{reason_code}",
        scope="ACCOUNT_CLERK",
        severity="error",
        blocks_new_exposure=True,
        allows_reduction=True,
        custody_owner="ACCOUNT_CLERK",
        strategy_instance_id=None,
        reason_code=reason_code,
        headline="Account episode",
        explanation="Account episode.",
        operator_impact="New entries are paused account-wide.",
        next_step="Review it.",
        observed_at_ms=observed_at_ms,
        evidence_age_ms=1_000,
        evidence_refs=("evidence",),
    )


def _flatten_actions(*uncertainties: ProjectedUncertainty) -> dict[str, RecoveryCapability]:
    context = _context(
        runs=(),
        positions=(
            ProjectedPosition(
                strategy_instance_id="spy-bot",
                symbol="SPY",
                attributed_qty=1.0,
                updated_at_ms=1_700_000_008_000,
            ),
        ),
        uncertainties=uncertainties,
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
    return {
        action.action_id: action
        for action in build_recovery_catalog(context)
        if action.action_id in {"prepare_safe_flatten", "execute_safe_flatten"}
    }


@pytest.mark.parametrize(
    "reason_code",
    ["UNFOLDABLE_BROKER_ORDER", "EXIT_NOT_FLAT", "EXIT_STUCK", "FAILED_ENTER_FILLED"],
)
def test_safe_flatten_stays_available_under_a_cause_whose_policy_admits_it(
    reason_code: str,
) -> None:
    """#2363: an unfoldable foreign order must not freeze safe-flatten.

    The admission is the registry's ``admits_safe_flatten`` flag, shared with
    the stuck/not-flat EXIT causes the executor exists to clear.
    """
    actions = _flatten_actions(_account_uncertainty(reason_code))

    assert actions["prepare_safe_flatten"].available is True
    assert actions["execute_safe_flatten"].available is True


@pytest.mark.parametrize(
    "reason_code", ["POSITION_DRIFT", "ORDER_OUTCOME_UNKNOWN", "UNREGISTERED_REASON"]
)
def test_safe_flatten_still_refuses_a_cause_that_does_not_admit_it(reason_code: str) -> None:
    """``allows_reduction`` alone never opens safe-flatten: POSITION_DRIFT admits
    a proven REDUCE, yet attributed quantities are exactly what it disputes."""
    actions = _flatten_actions(_account_uncertainty(reason_code))

    for action_id in ("prepare_safe_flatten", "execute_safe_flatten"):
        assert actions[action_id].available is False, action_id
        assert actions[action_id].unavailable_reason_code == "EXPOSURE_NOT_PROVEN"


def test_safe_flatten_refuses_an_unfoldable_order_newer_than_the_reconciliation() -> None:
    """#2363 review: an unfoldable order is broker-side evidence the flatten's
    reconciliation must postdate; admitting it is not ignoring it."""
    actions = _flatten_actions(
        _account_uncertainty("UNFOLDABLE_BROKER_ORDER", observed_at_ms=1_700_000_009_001)
    )

    for action_id in ("prepare_safe_flatten", "execute_safe_flatten"):
        assert actions[action_id].available is False, action_id
        assert actions[action_id].unavailable_reason_code == "UNCERTAINTY_EVIDENCE_NOT_RECONCILED"


@pytest.mark.parametrize("reason_code", ["EXIT_NOT_FLAT", "EXIT_STUCK", "FAILED_ENTER_FILLED"])
def test_safe_flatten_admits_an_exit_episode_refreshed_after_the_reconciliation(
    reason_code: str,
) -> None:
    """EXIT_NOT_FLAT / EXIT_STUCK are the Clerk's own EXIT bookkeeping, refreshed
    by every automatic re-drive; the flatten that clears them must not demand a
    newer reconciliation after each refresh (behaviour unchanged by #2363).
    FAILED_ENTER_FILLED (#2348) is derived from a folded fill, whose position
    change the position-evidence freshness gate already pins."""
    actions = _flatten_actions(_account_uncertainty(reason_code, observed_at_ms=1_700_000_009_500))

    assert actions["prepare_safe_flatten"].available is True
    assert actions["execute_safe_flatten"].available is True


def _bot_exit_episode(reason_code: str = "EXIT_STUCK") -> ProjectedUncertainty:
    return replace(
        _account_uncertainty(reason_code),
        scope="CUSTODY_SUBJECT",
        strategy_instance_id="spy-bot",
    )


def _residue_context(**overrides) -> RecoveryPolicyContext:
    """Bot +10 SPY stranded by an EXIT episode, run stopped, last pass drifted."""
    values = {
        "runs": (),
        "positions": (
            ProjectedPosition(
                strategy_instance_id="spy-bot",
                symbol="SPY",
                attributed_qty=10.0,
                updated_at_ms=1_700_000_008_000,
            ),
        ),
        "uncertainties": (_bot_exit_episode(),),
    }
    values.update(overrides)
    return _context(**values)


def _discharge(context: RecoveryPolicyContext) -> RecoveryCapability:
    return next(
        action
        for action in build_recovery_catalog(context)
        if action.action_id == "discharge_attributed_residue"
    )


@pytest.mark.parametrize("reason_code", ["EXIT_NOT_FLAT", "EXIT_STUCK"])
def test_residue_discharge_is_offered_for_a_residue_an_exit_episode_strands(
    reason_code: str,
) -> None:
    """#2381: the one cure for a flat broker under EXIT_NOT_FLAT/EXIT_STUCK."""
    action = _discharge(_residue_context(uncertainties=(_bot_exit_episode(reason_code),)))

    assert action.available is True
    assert action.execution_ref == "SPY"
    assert action.mutation is True
    assert action.confirmation is not None
    assert [item.reference for item in action.evidence] == [f"uncertainty:episode:{reason_code}"]


@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    [
        ({"strategy_instance_id": None}, "RECOVERY_SCOPE_UNSUPPORTED"),
        ({"positions": ()}, "RECOVERY_SCOPE_UNSUPPORTED"),
        ({"uncertainties": ()}, "NO_STRANDED_EXIT_EPISODE"),
        ({"uncertainties": (_account_uncertainty("POSITION_DRIFT"),)}, "NO_STRANDED_EXIT_EPISODE"),
        (
            {
                "uncertainties": (
                    _bot_exit_episode(),
                    _bot_exit_episode("EXECUTION_COVERAGE_CONFLICT"),
                )
            },
            "EXPOSURE_NOT_PROVEN",
        ),
        (
            {
                "runs": (
                    ProjectedRun(
                        run_id="run-1",
                        strategy_instance_id="spy-bot",
                        lifecycle_run_id="lifecycle-1",
                        state="ACTIVE",
                        started_at_ms=1_700_000_000_000,
                        stopped_at_ms=None,
                    ),
                )
            },
            "RUN_STILL_ACTIVE",
        ),
        (
            {
                "latest_account_reconciliation": ProjectedReconciliation(
                    reconciliation_id="reconciliation-17",
                    effect_operation_id=None,
                    order_ref=None,
                    trigger="operator",
                    attempted_at_ms=1_700_000_009_500,
                    outcome="RESOLVED_SUCCESS",
                    evidence_age_ms=500,
                    evidence_refs=("alpaca:positions:17",),
                )
            },
            "BROKER_AGREES_WITH_CUSTODY",
        ),
    ],
)
def test_residue_discharge_is_refused_outside_its_stranded_state(
    overrides: dict, reason_code: str
) -> None:
    action = _discharge(_residue_context(**overrides))

    assert action.available is False
    assert action.unavailable_reason_code == reason_code
    assert action.execution_ref is None


def test_a_write_off_not_offered_for_want_of_a_notice_names_every_notice_that_would_allow_it() -> None:
    from app.broker.alpaca.clerk.sqlite.uncertainty_policies import _REASON_POLICIES

    action = _discharge(_residue_context(uncertainties=()))

    allowing = [code for code, policy in _REASON_POLICIES.items() if policy.residue_discharge == "strands"]
    assert action.unavailable_reason_code == "NO_STRANDED_EXIT_EPISODE" and action.unavailable_reason is not None
    assert len(allowing) == 4 and all(code in action.unavailable_reason for code in allowing)


def test_residue_discharge_token_rejects_a_changed_residue() -> None:
    presented = _discharge(_residue_context())
    moved = _residue_context(
        positions=(
            ProjectedPosition(
                strategy_instance_id="spy-bot",
                symbol="SPY",
                attributed_qty=4.0,
                updated_at_ms=1_700_000_009_500,
            ),
        )
    )

    with pytest.raises(StaleRecoveryTokenError):
        recheck_recovery_action(
            moved,
            action_id="discharge_attributed_residue",
            concurrency_token=presented.concurrency_token,
        )


def test_a_reconciliation_older_than_the_residue_does_not_hide_the_discharge() -> None:
    """PR #2404 review: a clean pass before the EXIT episode proves nothing about it."""
    action = _discharge(
        _residue_context(
            latest_account_reconciliation=ProjectedReconciliation(
                reconciliation_id="reconciliation-16",
                effect_operation_id=None,
                order_ref=None,
                trigger="operator",
                attempted_at_ms=1_700_000_007_000,
                outcome="RESOLVED_SUCCESS",
                evidence_age_ms=3_000,
                evidence_refs=("alpaca:positions:16",),
            )
        )
    )

    assert action.available is True


# ── The Flatten gate accepts the Clerk's own check of the account (#2839) ────

_NOW_MS = 1_700_000_010_000  # ``_context``'s clock
_PASS_AT_MS = _NOW_MS - 5_000
_PASS_SAW_THROUGH = 40


def _clerk_pass(**changes: object) -> PublishedPass:
    """The Clerk's latest pass: five seconds old, vouching for the bot, seen through its newest record."""
    return replace(
        PublishedPass(vouches=True, observed_at_ms=_PASS_AT_MS, through_sequence=_PASS_SAW_THROUGH), **changes
    )


def _receipt(*, age_ms: int) -> ProjectedReconciliation:
    return ProjectedReconciliation(
        reconciliation_id="reconciliation-17",
        effect_operation_id=None,
        order_ref=None,
        trigger="OPERATOR_RECONCILE_NOW",
        attempted_at_ms=_NOW_MS - age_ms,
        outcome="RESOLVED_SUCCESS",
        evidence_age_ms=age_ms,
        evidence_refs=("fresh_account_snapshot",),
    )


def _stopped_holding(**overrides: object) -> RecoveryPolicyContext:
    """A bot stopped minutes ago holding 1 SPY, with no Reconcile now receipt, read beside the Clerk's pass."""
    values: dict[str, object] = {
        "runs": (
            ProjectedRun(
                run_id="run-1",
                strategy_instance_id="spy-bot",
                lifecycle_run_id="lifecycle-1",
                state="STOPPED",
                started_at_ms=_NOW_MS - 600_000,
                stopped_at_ms=_NOW_MS - 240_000,
            ),
        ),
        "positions": (
            ProjectedPosition(
                strategy_instance_id="spy-bot",
                symbol="SPY",
                attributed_qty=1.0,
                updated_at_ms=_NOW_MS - 300_000,
            ),
        ),
        "published_pass": _clerk_pass(),
        "subject_custody_sequence": _PASS_SAW_THROUGH,
    }
    values.update(overrides)
    return _context(**values)


def _flatten(context: RecoveryPolicyContext, action_id: str = "prepare_safe_flatten") -> RecoveryCapability:
    return next(action for action in build_recovery_catalog(context) if action.action_id == action_id)


def test_a_stopped_bots_flatten_rests_on_the_clerks_own_pass() -> None:
    """The Clerk checked the whole account five seconds ago and saw every record of this bot.

    Before #2839 the plan was refused until someone pressed Reconcile now.
    """
    context = _stopped_holding()

    prepare, execute = _flatten(context), _flatten(context, "execute_safe_flatten")

    assert prepare.available and execute.available
    plan = prepare.reduction_plan
    assert plan is not None
    assert (plan.account_check_kind, plan.account_check_observed_at_ms, plan.reconciliation_id) == (
        "clerk_pass",
        _PASS_AT_MS,
        None,
    )
    # The plan is good for the pass's own bound, counted from the pass -- not from when it was prepared.
    assert plan.expires_at_ms == _PASS_AT_MS + SWEEP_LIVENESS_BOUND_MS
    assert [(leg.symbol, leg.side, leg.quantity) for leg in plan.legs] == [("SPY", "sell", 1.0)]
    (evidence,) = prepare.evidence
    assert (evidence.reference, evidence.observed_at_ms, evidence.age_ms, evidence.freshness) == (
        f"clerk-pass:{_PASS_SAW_THROUGH}",
        _PASS_AT_MS,
        5_000,
        "fresh",
    )
    wire_plan = RecoveryCapabilityResponse.model_validate(prepare).model_dump(mode="json")["reduction_plan"]
    assert wire_plan["reconciliation_id"] is None
    assert wire_plan["account_check_kind"] == "clerk_pass"


def _an_unfoldable_order_seen_after_the_pass(*, later_by_ms: int = 1) -> ProjectedUncertainty:
    return _account_uncertainty("UNFOLDABLE_BROKER_ORDER", observed_at_ms=_PASS_AT_MS + later_by_ms)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"published_pass": None}, id="no-pass-was-read"),
        pytest.param({"published_pass": _clerk_pass(vouches=False)}, id="the-pass-does-not-vouch-for-this-bot"),
        pytest.param(
            {"published_pass": _clerk_pass(observed_at_ms=_NOW_MS - SWEEP_LIVENESS_BOUND_MS - 1)},
            id="the-pass-is-older-than-the-sweeps-bound",
        ),
        pytest.param(
            {"published_pass": _clerk_pass(observed_at_ms=_NOW_MS + 1)}, id="the-pass-is-dated-after-now"
        ),
        pytest.param(
            {"subject_custody_sequence": _PASS_SAW_THROUGH + 1},
            id="the-bot-wrote-a-custody-record-after-the-pass",
        ),
        pytest.param({"subject_custody_sequence": None}, id="the-bots-newest-record-was-not-read"),
        pytest.param(
            {
                "positions": (
                    ProjectedPosition(
                        strategy_instance_id="spy-bot", symbol="SPY", attributed_qty=1.0,
                        updated_at_ms=_PASS_AT_MS + 1,
                    ),
                )
            },
            id="the-position-changed-after-the-pass",
        ),
        pytest.param(
            {"uncertainties": (_an_unfoldable_order_seen_after_the_pass(),)},
            id="broker-evidence-arrived-after-the-pass",
        ),
        pytest.param(
            {"uncertainties": (_an_unfoldable_order_seen_after_the_pass(later_by_ms=0),)},
            id="broker-evidence-arrived-in-the-passs-own-millisecond",
        ),
        pytest.param({"runs": _context().runs}, id="the-bot-is-still-running"),
        pytest.param({"strategy_instance_id": None}, id="the-whole-account-not-one-bot"),
    ],
)
def test_a_pass_that_does_not_qualify_leaves_the_gate_exactly_as_it_was(overrides: dict[str, object]) -> None:
    """Refused, word for word and token for token, as if the Clerk's pass had never been read.

    The reason is whatever the receipt alone gives -- nothing new is said.
    """
    context = _stopped_holding(**overrides)
    unread = replace(context, published_pass=None, subject_custody_sequence=None)

    for action_id in ("prepare_safe_flatten", "execute_safe_flatten"):
        refused = _flatten(context, action_id)
        assert refused.available is False, action_id
        assert refused.reduction_plan is None
        assert refused == _flatten(unread, action_id)
    prepare = _flatten(context)
    if "uncertainties" not in overrides:
        assert (prepare.unavailable_reason_code, prepare.unavailable_reason, prepare.next_step) == (
            "CLEAN_RECONCILIATION_REQUIRED",
            "A successful account reconciliation is required before preparing reduction.",
            "Run Reconcile now and refresh the custody snapshot.",
        )


def test_a_pass_exactly_at_the_sweeps_bound_still_stands() -> None:
    context = _stopped_holding(published_pass=_clerk_pass(observed_at_ms=_NOW_MS - SWEEP_LIVENESS_BOUND_MS))

    assert _flatten(context).available


def test_a_fresh_receipt_wins_over_the_pass_and_the_plan_is_the_receipts_own() -> None:
    """With both standing nothing about a receipt-backed plan changes: not its evidence, expiry or token."""
    with_both = _stopped_holding(latest_account_reconciliation=_receipt(age_ms=1_000))
    receipt_only = replace(with_both, published_pass=None, subject_custody_sequence=None)

    for action_id in ("prepare_safe_flatten", "execute_safe_flatten"):
        assert _flatten(with_both, action_id) == _flatten(receipt_only, action_id), action_id
    plan = _flatten(with_both).reduction_plan
    assert plan is not None
    assert (plan.account_check_kind, plan.reconciliation_id, plan.account_check_observed_at_ms) == (
        "reconcile_now",
        "reconciliation-17",
        _NOW_MS - 1_000,
    )
    assert plan.expires_at_ms == _NOW_MS - 1_000 + FRESH_EVIDENCE_MAX_AGE_MS


def test_a_stale_receipt_does_not_stop_the_pass_from_being_accepted() -> None:
    context = _stopped_holding(latest_account_reconciliation=_receipt(age_ms=FRESH_EVIDENCE_MAX_AGE_MS + 1))

    plan = _flatten(context).reduction_plan

    assert plan is not None and plan.account_check_kind == "clerk_pass"


def test_a_plan_under_the_pass_outlives_the_next_pass_but_not_a_new_record_of_the_bot() -> None:
    """The token names the bot's newest custody record, never the pass.

    The Clerk publishes a new pass every 15 s; the plan the operator is
    looking at must survive that. A new record of the bot -- what a Flatten
    that failed leaves -- must end it, even under a pass that saw the record:
    the next plan is then a new EXIT that reads the account again.
    """
    presented = _stopped_holding()
    plan = _flatten(presented, "execute_safe_flatten")
    next_pass = replace(
        presented,
        now_ms=_NOW_MS + 15_000,
        published_pass=_clerk_pass(observed_at_ms=_NOW_MS + 10_000),
    )
    bot_moved = replace(
        next_pass,
        published_pass=_clerk_pass(observed_at_ms=_NOW_MS + 10_000, through_sequence=_PASS_SAW_THROUGH + 3),
        subject_custody_sequence=_PASS_SAW_THROUGH + 3,
    )

    renewed = recheck_recovery_action(
        next_pass, action_id="execute_safe_flatten", concurrency_token=plan.concurrency_token
    )

    assert renewed.reduction_plan is not None
    # The commit's plan expires with the pass that stands at the commit.
    assert renewed.reduction_plan.expires_at_ms == _NOW_MS + 10_000 + SWEEP_LIVENESS_BOUND_MS
    assert _flatten(bot_moved, "execute_safe_flatten").available
    with pytest.raises(StaleRecoveryTokenError):
        recheck_recovery_action(
            bot_moved, action_id="execute_safe_flatten", concurrency_token=plan.concurrency_token
        )


def test_a_flatten_presented_under_a_pass_is_refused_at_commit_once_the_pass_no_longer_stands() -> None:
    """The commit judges the pass it reads then, never the one the plan was presented under."""
    presented = _stopped_holding()
    plan = _flatten(presented, "execute_safe_flatten")
    assert plan.available

    for at_commit in (
        replace(presented, now_ms=_PASS_AT_MS + SWEEP_LIVENESS_BOUND_MS + 1),
        replace(presented, published_pass=_clerk_pass(vouches=False)),
        replace(presented, subject_custody_sequence=_PASS_SAW_THROUGH + 1),
        replace(presented, published_pass=None, subject_custody_sequence=None),
    ):
        assert _flatten(at_commit, "execute_safe_flatten").available is False
        with pytest.raises(StaleRecoveryTokenError):
            recheck_recovery_action(
                at_commit, action_id="execute_safe_flatten", concurrency_token=plan.concurrency_token
            )


def test_a_plan_presented_under_a_pass_is_never_committed_as_a_receipts_plan() -> None:
    """A Reconcile now landing between presentation and commit changes the evidence: the operator refreshes."""
    presented = _stopped_holding()
    plan = _flatten(presented, "execute_safe_flatten")
    reconciled = replace(presented, latest_account_reconciliation=_receipt(age_ms=0))

    assert _flatten(reconciled, "execute_safe_flatten").available
    with pytest.raises(StaleRecoveryTokenError):
        recheck_recovery_action(
            reconciled, action_id="execute_safe_flatten", concurrency_token=plan.concurrency_token
        )



# ── Settling a closing order whose outcome is unknown (#2854) ────────────────

_UNKNOWN_ORDER_SENT_AT_MS = 1_700_000_000_000


def _unknown_exit_order(**changes: object) -> ProjectedUnknownExitOrder:
    """A closing order to sell 10 SPY, sent once, whose answer was lost."""
    return replace(
        ProjectedUnknownExitOrder(
            effect_operation_id="exit-1",
            order_ref="bot:spy-bot:close-1",
            symbol="SPY",
            side="SELL",
            quantity=10.0,
            created_at_ms=_UNKNOWN_ORDER_SENT_AT_MS - 5,
            first_sent_at_ms=_UNKNOWN_ORDER_SENT_AT_MS,
            latest_send_sequence=41,
            lookup_wait_from_ms=_UNKNOWN_ORDER_SENT_AT_MS,
            answered_as_client_order_id=None,
            uncertainty_id="uncertainty:40",
        ),
        **changes,
    )


def _stopped_with_an_unknown_exit(**overrides: object) -> RecoveryPolicyContext:
    """A stopped bot whose closing order's wait has passed."""
    order = _unknown_exit_order()
    values: dict[str, object] = {
        "runs": (),
        "unknown_exit_orders": (order,),
        "now_ms": unknown_order_lookup_settles_at_ms(order),
    }
    values.update(overrides)
    return _context(**values)


def _settle(context: RecoveryPolicyContext) -> RecoveryCapability:
    return next(
        action
        for action in build_recovery_catalog(context)
        if action.action_id == "settle_unknown_exit_order"
    )


def test_settling_is_offered_for_a_stopped_bots_unknown_closing_order() -> None:
    action = _settle(_stopped_with_an_unknown_exit())

    assert action.available is True
    assert action.needed is True
    assert action.mutation is True
    assert action.confirmation is not None
    assert action.execution_ref == "bot:spy-bot:close-1"
    assert [item.reference for item in action.evidence] == ["order:bot:spy-bot:close-1"]


def test_settling_is_the_bots_cure_and_the_accounts_catalog_says_where_it_is_done() -> None:
    """The bot's page collects the attestation, so the action is pointed at.

    It is the bot's primary action -- what a roster row's command and a bot
    page's primary command are taken from: Reconcile now cannot settle an
    order no read shows. The account's catalog lists it, never available
    there, and says the order is settled from its bot's page.
    """
    bot = _stopped_with_an_unknown_exit(uncertainties=(_bot_exit_episode("ORDER_OUTCOME_UNKNOWN"),))
    account = replace(bot, strategy_instance_id=None)

    assert [action.action_id for action in build_recovery_catalog(bot) if action.primary] == [
        "settle_unknown_exit_order"
    ]
    # Still gated, it is no cure yet, and Reconcile now is the primary as before.
    still_running = replace(bot, runs=_context().runs)
    assert (_settle(still_running).available, _settle(still_running).primary) == (False, False)
    assert [action.action_id for action in build_recovery_catalog(still_running) if action.primary] == [
        "reconcile_now"
    ]
    on_the_desk = _settle(account)
    assert (on_the_desk.available, on_the_desk.primary) == (False, False)
    assert (on_the_desk.unavailable_reason_code, on_the_desk.needed) == ("NO_UNKNOWN_EXIT_ORDER", False)
    assert on_the_desk.unavailable_reason == "An uncertain closing order is settled from its bot's page."
    assert on_the_desk.next_step == "Open the bot whose closing order the Clerk could not confirm."
    assert on_the_desk.settlement is None


def test_a_flatten_refused_behind_an_unconfirmed_closing_order_says_to_settle_it() -> None:
    """A Flatten is refused while the bot's closing order is unconfirmed, and no reconciliation cures that.

    Its refusal used to say "Run Reconcile now", which finds nothing for an
    order no read shows. It names the settle action instead. Any other
    unknown outcome of the bot keeps the refusal it had.
    """
    held = _stopped_with_an_unknown_exit(
        positions=_residue_context().positions,
        uncertainties=(_bot_exit_episode("ORDER_OUTCOME_UNKNOWN"),),
    )

    def flatten(context: RecoveryPolicyContext) -> RecoveryCapability:
        return next(
            action for action in build_recovery_catalog(context) if action.action_id == "prepare_safe_flatten"
        )

    refused = flatten(held)
    assert (refused.available, refused.unavailable_reason_code) == (False, "EXPOSURE_NOT_PROVEN")
    assert refused.unavailable_reason == (
        "The Clerk sent a closing order for this bot and could not confirm it, so a flatten could "
        "close the position a second time."
    )
    assert refused.next_step == (
        "Settle that order first: look at this account's orders at Alpaca, then use "
        '"Settle the uncertain order".'
    )

    another_unknown = flatten(replace(held, unknown_exit_orders=()))
    assert (another_unknown.available, another_unknown.unavailable_reason_code) == (False, "EXPOSURE_NOT_PROVEN")
    assert another_unknown.unavailable_reason == (
        "Active uncertainty prevents the Clerk from proving a safe reduction quantity."
    )
    assert another_unknown.next_step == "Run Reconcile now and refresh the custody snapshot."


@pytest.mark.parametrize(
    ("unsettleable", "why_and_what_then"),
    [
        pytest.param(
            {"unknown_exit_orders": (_unknown_exit_order(answered_as_client_order_id=""),)},
            "Alpaca answered this order's send with an order that names no client order id, so the "
            "Clerk has no id to read that order by and cannot tell what it did. The Clerk keeps the "
            "position held. This action cannot settle the order. Look at this account's orders at "
            "Alpaca for the order it answered with. No action of the Clerk settles this order yet.",
            id="alpaca-answered-with-an-order-that-names-no-id",
        ),
        pytest.param(
            {"now_ms": unknown_order_lookup_settles_at_ms(_unknown_exit_order()) - 1},
            "The Clerk sent this order moments ago and may still find it at Alpaca by itself. Wait "
            "for the Clerk's next check of the account.",
            id="inside-the-wait-after-the-send",
        ),
    ],
)
def test_a_flatten_refused_behind_an_order_that_cannot_be_settled_yet_says_why_not(
    unsettleable: dict[str, object], why_and_what_then: str
) -> None:
    """The refusal sent the operator to "Settle the uncertain order" while that action was itself refused (#2854).

    The Flatten is refused for the same reason. Its next step is now the
    settle action's own reason and next step, under the action's name, taken
    from whether the catalog offers it for this order.
    """
    held = _stopped_with_an_unknown_exit(
        positions=_residue_context().positions,
        uncertainties=(_bot_exit_episode("ORDER_OUTCOME_UNKNOWN"),),
        **unsettleable,
    )
    catalog = {action.action_id: action for action in build_recovery_catalog(held)}
    settle, flatten = catalog["settle_unknown_exit_order"], catalog["prepare_safe_flatten"]

    assert (settle.available, settle.needed) == (False, True)
    assert f"{settle.unavailable_reason} {settle.next_step}" == why_and_what_then
    assert (flatten.available, flatten.unavailable_reason_code) == (False, "EXPOSURE_NOT_PROVEN")
    assert flatten.unavailable_reason == (
        "The Clerk sent a closing order for this bot and could not confirm it, so a flatten could "
        "close the position a second time."
    )
    assert flatten.next_step == f'"Settle the uncertain order" cannot be used now: {why_and_what_then}'


def test_the_settle_ticket_says_what_was_sent_and_offers_both_attestations() -> None:
    settlement = _settle(_stopped_with_an_unknown_exit()).settlement

    assert settlement is not None
    assert (settlement.symbol, settlement.side, settlement.quantity) == ("SPY", "sell", 10.0)
    assert settlement.ticket == (
        "The Clerk sent an order to sell 10 SPY on Tue Nov 14, 17:13 ET and could not confirm it. "
        "It holds the position and starts no other closing order for this bot. While the session "
        "the order was sent in is open, the Clerk sends this same order again by itself, after a "
        "wait that grows to 5 minutes and then stays there. If it does, look at Alpaca again and "
        "open this action again. Once that session has ended, the Clerk never sends the order again."
    )
    answered = _settle(
        _stopped_with_an_unknown_exit(
            unknown_exit_orders=(_unknown_exit_order(answered_as_client_order_id="another-id"),)
        )
    ).settlement
    assert answered is not None
    assert answered.ticket == (
        "The Clerk sent an order to sell 10 SPY on Tue Nov 14, 17:13 ET, and Alpaca answered under "
        "another order id. The Clerk holds the position and starts no other closing order for this "
        "bot, and does not send this order again."
    )
    assert [(choice.attestation, choice.asks_broker_order_id) for choice in settlement.choices] == [
        ("absent", False),
        ("exists", True),
    ]
    # What "no such order" leaves the operator to do is said before they choose it and
    # again as they confirm: the Clerk sells nothing by itself afterwards.
    assert settlement.choices[0].explanation == (
        "The Clerk checks once more that this order is not at Alpaca and that nothing is working on "
        "SPY. If so, it ends this exit as not sent. The shares stay with the bot, and the Clerk "
        "sends no order for them by itself: you then sell them or write them off from this page. "
        "This cannot be undone."
    )
    confirmation = _settle(_stopped_with_an_unknown_exit()).confirmation
    assert confirmation is not None
    assert confirmation.explanation == (
        "The Clerk reads Alpaca again and settles the order only if what it reads agrees with what "
        "you said. It sends no order. If you said Alpaca has no such order, the position stays with "
        "the bot and the Clerk sends no order for it by itself: you then close it or write it off "
        "from this page. This cannot be undone."
    )
    # The wire carries it to the page that asks the operator.
    assert RecoveryCapabilityResponse.model_validate(
        _settle(_stopped_with_an_unknown_exit())
    ).settlement is not None


@pytest.mark.parametrize(
    ("overrides", "reason_code", "needed"),
    [
        ({"unknown_exit_orders": ()}, "NO_UNKNOWN_EXIT_ORDER", False),
        # A Dry Run's and a Shadow's orders reach no broker an operator could look at.
        ({"account_id": "sim:spy-bot"}, "NO_UNKNOWN_EXIT_ORDER", False),
        ({"account_id": "shadow:PA-LIVE"}, "NO_UNKNOWN_EXIT_ORDER", False),
        (
            {
                "runs": (
                    ProjectedRun(
                        run_id="run-1",
                        strategy_instance_id="spy-bot",
                        lifecycle_run_id="lifecycle-1",
                        state="ACTIVE",
                        started_at_ms=1_700_000_000_000,
                        stopped_at_ms=None,
                    ),
                )
            },
            "RUN_STILL_ACTIVE",
            True,
        ),
        (
            {"now_ms": unknown_order_lookup_settles_at_ms(_unknown_exit_order()) - 1},
            "ORDER_LOOKUP_STILL_SETTLING",
            True,
        ),
        (
            {"unknown_exit_orders": (_unknown_exit_order(answered_as_client_order_id=""),)},
            "ANSWERED_ORDER_NAMES_NO_ID",
            True,
        ),
    ],
)
def test_settling_is_refused_outside_its_state(
    overrides: dict[str, object], reason_code: str, needed: bool
) -> None:
    action = _settle(_stopped_with_an_unknown_exit(**overrides))

    assert action.available is False
    assert action.unavailable_reason_code == reason_code
    assert action.needed is needed
    assert action.execution_ref is None
    assert action.settlement is None


def test_an_order_answered_under_another_client_order_id_is_offered_without_the_wait() -> None:
    """The Clerk never sends it again or finds it by itself, so there is nothing to wait for."""
    answered = _unknown_exit_order(answered_as_client_order_id="another-id")

    action = _settle(
        _stopped_with_an_unknown_exit(
            unknown_exit_orders=(answered,), now_ms=answered.lookup_wait_from_ms
        )
    )

    assert action.available is True


def test_an_order_answered_with_no_client_order_id_is_not_offered_and_the_reason_says_why() -> None:
    """The regression (#2854 review): Alpaca answered the send with an order that names no client order id.

    The action was offered, and neither choice could run: "no such order"
    refuses with no id to read that order by, and "Alpaca shows it" takes up
    only an order under the id that was sent. It is unavailable now, in
    words that say what happened, that the Clerk keeps the position, and
    that this action cannot settle it -- for a running bot too, which is not
    told to stop for an action that would still not run.
    """
    unnamed = _unknown_exit_order(answered_as_client_order_id="")
    running = ProjectedRun(
        run_id="run-1", strategy_instance_id="spy-bot", lifecycle_run_id="lifecycle-1",
        state="ACTIVE", started_at_ms=1_700_000_000_000, stopped_at_ms=None,
    )

    for runs in ((), (running,)):
        action = _settle(_stopped_with_an_unknown_exit(unknown_exit_orders=(unnamed,), runs=runs))

        assert (action.available, action.needed) == (False, True)
        assert action.unavailable_reason_code == "ANSWERED_ORDER_NAMES_NO_ID"
        assert action.unavailable_reason == (
            "Alpaca answered this order's send with an order that names no client order id, so the "
            "Clerk has no id to read that order by and cannot tell what it did. The Clerk keeps the "
            "position held. This action cannot settle the order."
        )
        assert action.next_step == (
            "Look at this account's orders at Alpaca for the order it answered with. No action of "
            "the Clerk settles this order yet."
        )
        assert (action.execution_ref, action.settlement) == (None, None)


def test_the_settle_token_outlives_a_pass_and_not_a_send_made_again() -> None:
    """A pass changes the clock and the episode's time, never the order's latest send."""
    presented = _stopped_with_an_unknown_exit()
    token = _settle(presented).concurrency_token
    a_pass_later = replace(
        presented,
        now_ms=presented.now_ms + 15_000,
        control_revision=presented.control_revision + 1,
        uncertainties=(replace(_bot_exit_episode("ORDER_OUTCOME_UNKNOWN"), observed_at_ms=presented.now_ms + 15_000),),
    )

    assert recheck_recovery_action(
        a_pass_later, action_id="settle_unknown_exit_order", concurrency_token=token
    ).available

    for changed in (
        _unknown_exit_order(latest_send_sequence=57),
        _unknown_exit_order(answered_as_client_order_id="another-id"),
        _unknown_exit_order(uncertainty_id="uncertainty:99"),
        _unknown_exit_order(order_ref="bot:spy-bot:close-2"),
    ):
        with pytest.raises(StaleRecoveryTokenError):
            recheck_recovery_action(
                replace(a_pass_later, unknown_exit_orders=(changed,)),
                action_id="settle_unknown_exit_order",
                concurrency_token=token,
            )


# ── the owner's ending of an expired option contract (ADR 0076 decision 22) ──

# One SPY call that expired on Friday 2023-11-03, held past that by a Dry Run's bot.
_EXPIRED_CALL = "SPY231103C00430000"


def _not_settled_hold(sid: str, *, contract: str = _EXPIRED_CALL) -> ProjectedUncertainty:
    return ProjectedUncertainty(
        uncertainty_id=f"option-events:{sid}:OPTION_EXPIRED_NOT_SETTLED",
        scope="CUSTODY_SUBJECT",
        severity="warning",
        blocks_new_exposure=True,
        allows_reduction=True,
        custody_owner="ACCOUNT_CLERK",
        strategy_instance_id=sid,
        reason_code="OPTION_EXPIRED_NOT_SETTLED",
        headline="An expired contract was not settled",
        explanation="The simulation kept no price of SPY from the contract's last session.",
        operator_impact="New entries are paused for this bot.",
        next_step="End the contract at your word.",
        observed_at_ms=1_700_000_009_000,
        evidence_age_ms=1_000,
        evidence_refs=(f"contract:{contract}",),
    )


def _ending_offered_with(*holds: ProjectedUncertainty) -> RecoveryCapability:
    context = _context(
        account_id="sim:spy-bot",
        positions=(
            ProjectedPosition(
                strategy_instance_id="spy-bot", symbol=_EXPIRED_CALL, attributed_qty=1.0,
                updated_at_ms=1_700_000_008_000,
            ),
        ),
        uncertainties=holds,
    )
    return next(action for action in build_recovery_catalog(context) if action.action_id == "end_expired_option")


def test_an_ending_is_offered_on_this_bots_own_hold_that_names_the_contract_and_on_no_other_hold() -> None:
    """The hold is the durable word that a whole look found no settlement of this bot's contract. Another
    bot's hold says that of the other bot's position, and a hold naming another contract says it of that one."""
    assert _ending_offered_with(_not_settled_hold("spy-bot")).available

    for another in (
        _not_settled_hold("another-bot"),
        _not_settled_hold("spy-bot", contract="SPY231103P00430000"),
    ):
        ending = _ending_offered_with(another)

        assert not ending.available and ending.needed
        assert ending.unavailable_reason_code == "EXPIRED_OPTION_CONTRACT_MAY_STILL_BE_SETTLED"
