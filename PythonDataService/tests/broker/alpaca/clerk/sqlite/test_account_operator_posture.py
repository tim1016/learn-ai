"""Decision-table tests for the canonical account operator posture (#1664).

Every scenario in the issue's acceptance criteria gets its own test: healthy,
review-only uncertainty, available recovery, unavailable/waiting recovery,
terminal authority failure, inactive account, wrong execution mode,
unresolved intents, and missing/stale/unhealthy Clerk channels. A separate
contract test proves the ``account_desk``/``fleet_roster`` projections share
condition identity/severity while carrying host-relative disposition/copy/
moves, per ADR 0027.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.broker.alpaca.clerk.sqlite.account_operator_posture import (
    AccountOperatorPostureContext,
    build_account_operator_posture,
)
from app.broker.alpaca.clerk.sqlite.projection_models import ProjectionGuidance, RecoveryCapability
from app.schemas.operator_blocker import AccountOperatorPosture


def _guidance(**overrides: Any) -> ProjectionGuidance:
    values: dict[str, Any] = {
        "headline": "Account Clerk custody is healthy",
        "explanation": "Durable Clerk state has no active hold or unresolved uncertainty in this scope.",
        "scope": "ACCOUNT_CLERK",
        "impact": "Normal Clerk-governed controls remain available.",
        "custody_owner": "ACCOUNT_CLERK",
        "may_create_exposure": True,
        "available_safety_actions": (),
        "action_required": False,
        "next_step": "No recovery action is required.",
    }
    values.update(overrides)
    return ProjectionGuidance(**values)


def _recovery_action(**overrides: Any) -> RecoveryCapability:
    values: dict[str, Any] = {
        "action_id": "reconcile_now",
        "label": "Reconcile now",
        "explanation": "Compare Clerk custody to a fresh broker observation.",
        "available": True,
        "unavailable_reason_code": None,
        "unavailable_reason": None,
        "scope": "ACCOUNT_CLERK",
        "freshness": "fresh",
        "evidence": (),
        "reduction_plan": None,
        "confirmation": None,
        "next_step": "Run reconciliation against the current observation.",
        "concurrency_token": "token-1",
        "execution_ref": None,
        "mutation": True,
        "primary": True,
    }
    values.update(overrides)
    return RecoveryCapability(**values)


def _context(**overrides: Any) -> AccountOperatorPostureContext:
    values: dict[str, Any] = {
        "authority_health": "healthy",
        "uncertainty_count": 0,
        "guidance": _guidance(),
        "recovery_actions": (),
        "account_mode": "paper",
        "account_status": "ACTIVE",
        "trading_blocked": False,
        "account_blocked": False,
        "outstanding_intents": 0,
        "channels_ready": True,
        "channels_detail": None,
    }
    values.update(overrides)
    return AccountOperatorPostureContext(**values)


def test_healthy_evidence_yields_null_condition_and_backend_status_copy() -> None:
    posture = build_account_operator_posture(_context())

    assert posture.condition is None
    assert posture.account_desk is None
    assert posture.fleet_roster is None
    assert posture.status_headline == "Account Clerk custody is healthy"
    assert posture.status_detail is not None


def test_review_only_uncertainty_waits_without_a_move() -> None:
    posture = build_account_operator_posture(
        _context(
            uncertainty_count=1,
            guidance=_guidance(
                headline="Execution evidence needs review",
                explanation="One broker outcome is not yet proven.",
                action_required=False,
            ),
            recovery_actions=(),
        )
    )

    assert posture.condition is not None
    assert posture.condition.severity == "warning"
    assert posture.account_desk is not None
    assert posture.account_desk.disposition == "wait"
    assert posture.account_desk.primary_move is None
    assert posture.fleet_roster is not None
    assert posture.fleet_roster.disposition == "wait"
    assert posture.fleet_roster.primary_move is None


def test_available_recovery_is_fix_here_on_desk_and_fix_elsewhere_on_roster() -> None:
    posture = build_account_operator_posture(
        _context(
            uncertainty_count=1,
            guidance=_guidance(
                headline="Clerk reconciliation required",
                explanation="The Clerk needs a fresh broker observation.",
                action_required=False,
            ),
            recovery_actions=(_recovery_action(available=True, primary=True),),
        )
    )

    assert posture.condition is not None
    assert posture.condition.severity == "blocking"
    assert posture.account_desk is not None
    assert posture.account_desk.disposition == "fix_here"
    assert posture.account_desk.primary_move is not None
    assert posture.account_desk.primary_move.action.kind == "confirm_in_form"
    assert posture.fleet_roster is not None
    assert posture.fleet_roster.disposition == "fix_elsewhere"
    assert posture.fleet_roster.primary_move is not None
    assert posture.fleet_roster.primary_move.action.kind == "navigate"


def test_unavailable_recovery_on_unhealthy_authority_waits_blocking() -> None:
    posture = build_account_operator_posture(
        _context(
            authority_health="degraded_to_mirror",
            uncertainty_count=1,
            guidance=_guidance(
                headline="Alpaca execution is paused",
                explanation="The Account Clerk authority is unavailable and cannot prove safe execution.",
                action_required=True,
            ),
            recovery_actions=(
                _recovery_action(
                    available=False,
                    primary=False,
                    action_id="rebuild_from_mirror",
                    unavailable_reason_code="EVIDENCE_STALE",
                    unavailable_reason="Mirror evidence is not fresh enough yet.",
                ),
            ),
        )
    )

    assert posture.condition is not None
    assert posture.condition.severity == "blocking"
    assert posture.account_desk is not None
    assert posture.account_desk.disposition == "wait"
    assert posture.account_desk.primary_move is None
    assert posture.fleet_roster is not None
    assert posture.fleet_roster.disposition == "wait"


def test_terminal_authority_failure_with_no_recovery_action_is_terminal_on_both_hosts() -> None:
    posture = build_account_operator_posture(
        _context(
            authority_health="failed",
            uncertainty_count=0,
            guidance=_guidance(
                headline="Alpaca execution is paused",
                explanation="The Account Clerk authority is unavailable and cannot prove safe execution.",
                action_required=True,
            ),
            recovery_actions=(),
        )
    )

    assert posture.condition is not None
    assert posture.condition.severity == "blocking"
    assert posture.account_desk is not None
    assert posture.account_desk.disposition == "terminal"
    assert posture.account_desk.primary_move is not None or posture.account_desk.secondary_moves
    assert posture.fleet_roster is not None
    assert posture.fleet_roster.disposition == "terminal"


def test_inactive_account_waits_with_account_status_evidence() -> None:
    posture = build_account_operator_posture(_context(account_status="ACCOUNT_UPDATED"))

    assert posture.condition is not None
    assert posture.condition.id == "alpaca_account_inactive"
    assert posture.condition.evidence["account_status"] == "ACCOUNT_UPDATED"
    assert posture.account_desk is not None
    assert posture.account_desk.disposition == "wait"
    assert "not active" in posture.account_desk.headline


def test_wrong_execution_mode_is_terminal_with_a_real_cure_not_an_indefinite_wait() -> None:
    """A non-paper account is a static config problem — no fresh evidence
    resolves it, so it must not sit in `wait` forever (2026-08-20 review)."""
    posture = build_account_operator_posture(_context(account_mode="live"))

    assert posture.condition is not None
    assert posture.condition.id == "alpaca_account_wrong_execution_mode"
    assert posture.condition.severity == "blocking"
    assert posture.condition.evidence["account_mode"] == "live"
    assert posture.account_desk is not None
    assert posture.account_desk.disposition == "terminal"
    assert posture.account_desk.primary_move is not None
    assert posture.account_desk.primary_move.action.kind == "open_runbook"
    assert "paper mode" in posture.account_desk.headline
    assert posture.fleet_roster is not None
    assert posture.fleet_roster.disposition == "terminal"


def test_account_identity_mismatch_is_terminal_and_takes_priority_over_stale_defaults() -> None:
    """A mismatched account read must never leak its facts into this
    projection's account — the eligibility fields stay None regardless of
    what the (wrong) account's mode/status/block flags actually were."""
    posture = build_account_operator_posture(
        _context(
            account_identity_mismatch=True,
            account_mode=None,
            account_status=None,
            trading_blocked=None,
            account_blocked=None,
        )
    )

    assert posture.condition is not None
    assert posture.condition.id == "alpaca_account_identity_mismatch"
    assert posture.condition.severity == "blocking"
    assert posture.account_desk is not None
    assert posture.account_desk.disposition == "terminal"
    assert posture.account_desk.primary_move is not None
    assert posture.account_desk.primary_move.action.kind == "open_runbook"
    assert posture.fleet_roster is not None
    assert posture.fleet_roster.disposition == "terminal"


def test_trading_blocked_takes_priority_over_channels_and_intents() -> None:
    posture = build_account_operator_posture(
        _context(trading_blocked=True, channels_ready=False, outstanding_intents=3)
    )

    assert posture.condition is not None
    assert posture.condition.id == "alpaca_account_trading_blocked"


def test_missing_stale_or_unhealthy_channels_block_before_intents() -> None:
    posture = build_account_operator_posture(
        _context(
            channels_ready=False,
            channels_detail="Clerk submission channels are not ready (unhealthy: execution).",
            outstanding_intents=2,
        )
    )

    assert posture.condition is not None
    assert posture.condition.id == "alpaca_account_channels_not_ready"
    assert posture.condition.severity == "blocking"
    assert posture.account_desk is not None
    assert "execution" in (posture.account_desk.detail or "")


def test_unresolved_intents_wait_as_a_warning() -> None:
    posture = build_account_operator_posture(_context(outstanding_intents=2))

    assert posture.condition is not None
    assert posture.condition.id == "alpaca_account_unresolved_intents"
    assert posture.condition.severity == "warning"
    assert posture.account_desk is not None
    assert posture.account_desk.disposition == "wait"


def test_missing_account_snapshot_reports_evidence_unavailable() -> None:
    posture = build_account_operator_posture(
        _context(
            account_mode=None,
            account_status=None,
            trading_blocked=None,
            account_blocked=None,
        )
    )

    assert posture.condition is not None
    assert posture.condition.id == "alpaca_account_evidence_unavailable"
    assert posture.condition.severity == "warning"


@pytest.mark.parametrize(
    "overrides",
    [
        {"account_mode": None},
        {"account_status": None},
        {"trading_blocked": None},
        {"account_blocked": None},
    ],
)
def test_partial_none_account_fields_are_unrepresentable(overrides: dict[str, Any]) -> None:
    """A missing account snapshot must leave all four fields unset — never a
    partial mix that could accidentally read a stale/defaulted value on one
    field while treating another as authoritative (2026-08-19 review)."""
    with pytest.raises(ValueError, match="all present or all None"):
        _context(**overrides)


def test_custody_condition_takes_priority_over_account_eligibility() -> None:
    posture = build_account_operator_posture(
        _context(
            uncertainty_count=1,
            guidance=_guidance(action_required=False),
            recovery_actions=(_recovery_action(available=True, primary=True),),
            account_mode="live",
        )
    )

    assert posture.condition is not None
    assert posture.condition.id.startswith("alpaca_clerk_recovery:")


def test_both_host_projections_share_condition_identity_and_severity() -> None:
    posture = build_account_operator_posture(
        _context(
            uncertainty_count=1,
            guidance=_guidance(action_required=False),
            recovery_actions=(_recovery_action(available=True, primary=True),),
        )
    )

    assert posture.account_desk is not None
    assert posture.fleet_roster is not None
    assert posture.account_desk.condition.id == posture.fleet_roster.condition.id
    assert posture.account_desk.condition.severity == posture.fleet_roster.condition.severity
    # Host-relative disposition/copy/moves differ even though identity matches.
    assert posture.account_desk.disposition != posture.fleet_roster.disposition
    assert posture.account_desk.primary_move != posture.fleet_roster.primary_move


@pytest.mark.parametrize("field", ["account_desk", "fleet_roster"])
def test_healthy_posture_cannot_carry_a_host_blocker(field: str) -> None:
    blocked_posture = build_account_operator_posture(
        _context(
            uncertainty_count=1,
            guidance=_guidance(action_required=False),
            recovery_actions=(_recovery_action(available=True, primary=True),),
        )
    )
    stray_blocker = getattr(blocked_posture, field)
    assert stray_blocker is not None

    with pytest.raises(ValidationError, match="must not carry a host blocker"):
        AccountOperatorPosture(
            condition=None,
            account_desk=stray_blocker if field == "account_desk" else None,
            fleet_roster=stray_blocker if field == "fleet_roster" else None,
            status_headline="Account Clerk custody is healthy",
            status_detail=None,
        )


def test_mismatched_host_blocker_condition_is_rejected() -> None:
    mismatched_posture = build_account_operator_posture(_context(outstanding_intents=1))
    other_posture = build_account_operator_posture(_context(account_mode="live"))
    assert mismatched_posture.condition is not None
    assert mismatched_posture.account_desk is not None
    assert other_posture.fleet_roster is not None

    with pytest.raises(ValidationError, match="must match the posture's condition"):
        AccountOperatorPosture(
            condition=mismatched_posture.condition,
            account_desk=mismatched_posture.account_desk,
            fleet_roster=other_posture.fleet_roster,
            status_headline="mismatch",
            status_detail=None,
        )


@pytest.mark.parametrize("missing_field", ["account_desk", "fleet_roster"])
def test_non_null_condition_requires_both_host_projections(missing_field: str) -> None:
    posture = build_account_operator_posture(_context(outstanding_intents=1))
    assert posture.condition is not None
    assert posture.account_desk is not None
    assert posture.fleet_roster is not None

    with pytest.raises(ValidationError, match="requires both the account_desk and fleet_roster"):
        AccountOperatorPosture(
            condition=posture.condition,
            account_desk=None if missing_field == "account_desk" else posture.account_desk,
            fleet_roster=None if missing_field == "fleet_roster" else posture.fleet_roster,
            status_headline="partial",
            status_detail=None,
        )


def test_host_projection_must_carry_the_matching_host_field() -> None:
    posture = build_account_operator_posture(_context(outstanding_intents=1))
    assert posture.condition is not None
    assert posture.fleet_roster is not None

    with pytest.raises(ValidationError, match="must carry host="):
        AccountOperatorPosture(
            condition=posture.condition,
            # host="fleet_roster" placed in the account_desk slot.
            account_desk=posture.fleet_roster,
            fleet_roster=posture.fleet_roster,
            status_headline="mismatch",
            status_detail=None,
        )
