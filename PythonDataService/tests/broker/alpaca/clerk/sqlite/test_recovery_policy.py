"""Typed recovery catalog and action-token policy tests (#1395)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.broker.alpaca.clerk.sqlite.projection_models import (
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
        "operations": (),
        "positions": (),
        "uncertainties": (),
        "latest_reconciliation": None,
    }
    values.update(overrides)
    return RecoveryPolicyContext(**values)


def test_healthy_catalog_omits_failure_and_generic_recovery_actions() -> None:
    actions = {action.action_id for action in build_recovery_catalog(_context())}

    assert actions == {
        "reconcile_now",
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


def test_bot_uncertainty_authors_scope_impact_and_next_step() -> None:
    uncertainty = ProjectedUncertainty(
        uncertainty_id="uncertain-1",
        scope="BOT",
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

    assert guidance.scope == "BOT"
    assert guidance.may_create_exposure is False
    assert guidance.impact == "Only this bot cannot create exposure."
    assert guidance.next_step == "The Clerk is reconciling automatically."
