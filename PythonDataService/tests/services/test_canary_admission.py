"""Unit coverage for the guarded Alpaca Paper canary gate (issue #1729).

`tests/services/test_run_admission.py` exercises the composed gate through
`evaluate_run_admission`; this file tests the pure building blocks in
isolation: the allowlist's shipped emptiness, exact-pairing membership, gate
applicability, and the Clerk-proved rollback boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.services.strategy_validation_admission as validation_admission
from app.schemas.canary_admission import CanaryActivationPlan, CanaryRollbackDecision
from app.schemas.strategy_validation import StrategyValidationFlagRequest
from app.services.broker_v2_panel.strategy_catalog import compose_strategy_catalog
from app.services.canary_admission import (
    CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS,
    CanaryActivationRefused,
    CanaryAdmissionLedgerError,
    active_canary_pairings,
    apply_canary_activation,
    canary_gate_applies,
    canary_pairing_admitted,
    evaluate_canary_rollback,
    plan_canary_activation,
    revoke_canary_pairing,
)
from app.services.strategy_validation_manifest import (
    append_strategy_validation_flag_event,
    strategy_registry_seeds,
)

_NOW = 1_700_000_010_000


def test_canary_allowlist_ships_empty() -> None:
    """#1729 safety constraint: no production entry may ever slip in without
    this test failing. Operational activation lives in the audited local
    ledger and never mutates this source constant, adds a default, or falls
    back to an environment variable."""
    assert frozenset() == CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS


@pytest.mark.parametrize(
    ("mode", "program_build_state", "expected"),
    [
        ("trade", "PROVEN", True),
        ("trade", "UNPROVEN", False),
        ("trade", "NOT_APPLICABLE", False),
        ("dry_run", "PROVEN", False),
        ("log_only", "PROVEN", False),
    ],
)
def test_canary_gate_applies_only_to_proven_trade_mode_signal_programs(
    mode: str, program_build_state: str, expected: bool
) -> None:
    assert canary_gate_applies(mode=mode, program_build_state=program_build_state) is expected


def test_canary_pairing_admitted_matches_only_the_exact_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", "paper-account")}),
    )

    assert canary_pairing_admitted(program_key="ema_crossover_signal", account_id="paper-account") is True
    assert canary_pairing_admitted(program_key="ema_crossover_signal", account_id="other-account") is False
    assert canary_pairing_admitted(program_key="sma_crossover_signal", account_id="paper-account") is False


def test_canary_pairing_admitted_is_false_against_the_empty_shipped_allowlist() -> None:
    assert canary_pairing_admitted(program_key="ema_crossover_signal", account_id="paper-account") is False


def test_plan_canary_activation_is_read_only_and_binds_current_ema_proof(tmp_path: Path) -> None:
    ledger_path = tmp_path / "canary-admission.json"

    plan = plan_canary_activation(
        program_key="ema_crossover_signal",
        account_id="paper-account",
        actor="local:test-operator",
        reason="Begin the reviewed one-share EMA paper canary.",
        ledger_path=ledger_path,
        confirmation_ttl_ms=120_000,
        clock=lambda: _NOW,
    )

    assert isinstance(plan, CanaryActivationPlan)
    assert plan.program_key == "ema_crossover_signal"
    assert plan.account_id == "paper-account"
    assert plan.created_at_ms == _NOW
    assert plan.expires_at_ms == _NOW + 120_000
    assert plan.evidence.validation_event_id
    assert plan.evidence.validation_snapshot_sha256
    assert plan.evidence.qualification_receipt_hash
    assert plan.evidence.running_artifact_digest
    assert plan.plan_id == plan.confirmation_token
    assert ledger_path.exists() is False


def test_apply_canary_activation_admits_only_the_exact_pair(tmp_path: Path) -> None:
    ledger_path = tmp_path / "canary-admission.json"
    plan = _ema_plan(ledger_path)

    event = apply_canary_activation(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        ledger_path=ledger_path,
        clock=lambda: _NOW + 1,
    )

    assert event.action == "activated"
    assert event.program_key == "ema_crossover_signal"
    assert event.account_id == "paper-account"
    assert canary_pairing_admitted(
        program_key="ema_crossover_signal",
        account_id="paper-account",
        ledger_path=ledger_path,
    ) is True
    assert canary_pairing_admitted(
        program_key="ema_crossover_signal",
        account_id="other-account",
        ledger_path=ledger_path,
    ) is False
    assert canary_pairing_admitted(
        program_key="sma_crossover",
        account_id="paper-account",
        ledger_path=ledger_path,
    ) is False


def test_operational_harness_acceptance_reaches_paper_catalog_and_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One category policy governs Accept through Paper and Start admission."""
    account_id = "harness-paper-account"
    ledger_path = tmp_path / "canary-admission.json"
    entry = append_strategy_validation_flag_event(
        "deployment_validation",
        StrategyValidationFlagRequest(
            flag="validated",
            reason="Accept the deterministic deployment harness qualification.",
        ),
        strategy_registry_seeds(),
        flag_events_path=tmp_path / "flag-events.json",
        flagged_by="local:test-operator",
        now_ms=1_900_000_000_000,
    )
    assert entry.current_flag_event is not None
    assert entry.current_flag_event.evidence_snapshot.qc_cloud_backtest_id is None
    assert entry.current_flag_event.evidence_snapshot.validator_code_ref is None
    assert entry.current_flag_event.evidence_snapshot.audit_copy_ref is None

    fact = validation_admission.current_strategy_validation_fact(
        SimpleNamespace(strategy_key=entry.strategy_key, evidence_override=None),
        _NOW,
        entries_loader=lambda: [entry],
    )
    assert fact.state == "VERIFIED"

    monkeypatch.setattr(validation_admission, "_load_current_entries", lambda: [entry])
    monkeypatch.setattr(
        "app.services.canary_admission.DEFAULT_CANARY_ADMISSION_LEDGER_PATH",
        ledger_path,
    )
    plan = plan_canary_activation(
        program_key=entry.strategy_key,
        account_id=account_id,
        actor="local:test-operator",
        reason="Review the deployment harness Paper pairing.",
        ledger_path=ledger_path,
        clock=lambda: _NOW,
    )
    apply_canary_activation(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        ledger_path=ledger_path,
        clock=lambda: _NOW + 1,
    )

    [row] = compose_strategy_catalog([entry], account_id=account_id)
    assert row.paper_access_state == "enabled"
    assert row.evidence_status == "accepted"
    assert row.selectable is True


def test_apply_canary_activation_rejects_wrong_confirmation_token(tmp_path: Path) -> None:
    ledger_path = tmp_path / "canary-admission.json"
    plan = _ema_plan(ledger_path)

    with pytest.raises(CanaryActivationRefused, match="confirmation token"):
        apply_canary_activation(
            plan=plan,
            confirmation_token="0" * 64,
            ledger_path=ledger_path,
            clock=lambda: _NOW + 1,
        )

    assert ledger_path.exists() is False


def test_apply_canary_activation_rejects_a_different_ledger_than_the_reviewed_plan(
    tmp_path: Path,
) -> None:
    reviewed_ledger_path = tmp_path / "reviewed-canary-admission.json"
    different_ledger_path = tmp_path / "different-canary-admission.json"
    plan = _ema_plan(reviewed_ledger_path)

    with pytest.raises(CanaryActivationRefused, match="different canary admission ledger"):
        apply_canary_activation(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            ledger_path=different_ledger_path,
            clock=lambda: _NOW + 1,
        )

    assert reviewed_ledger_path.exists() is False
    assert different_ledger_path.exists() is False


def test_apply_canary_activation_rejects_an_expired_plan(tmp_path: Path) -> None:
    ledger_path = tmp_path / "canary-admission.json"
    plan = _ema_plan(ledger_path)

    with pytest.raises(CanaryActivationRefused, match="expired"):
        apply_canary_activation(
            plan=plan,
            confirmation_token=plan.confirmation_token,
            ledger_path=ledger_path,
            clock=lambda: plan.expires_at_ms + 1,
        )

    assert ledger_path.exists() is False


def test_apply_canary_activation_rechecks_a_stale_ledger_head(tmp_path: Path) -> None:
    ledger_path = tmp_path / "canary-admission.json"
    first_plan = _ema_plan(ledger_path)
    stale_plan = _ema_plan(ledger_path)
    apply_canary_activation(
        plan=first_plan,
        confirmation_token=first_plan.confirmation_token,
        ledger_path=ledger_path,
        clock=lambda: _NOW + 1,
    )

    with pytest.raises(CanaryActivationRefused, match="ledger changed"):
        apply_canary_activation(
            plan=stale_plan,
            confirmation_token=stale_plan.confirmation_token,
            ledger_path=ledger_path,
            clock=lambda: _NOW + 2,
        )


def test_plan_canary_activation_refuses_evidence_only_strategy(tmp_path: Path) -> None:
    with pytest.raises(CanaryActivationRefused, match="accepted validation proof"):
        plan_canary_activation(
            program_key="rsi_mean_reversion",
            account_id="paper-account",
            actor="local:test-operator",
            reason="This evidence-only strategy must remain blocked.",
            ledger_path=tmp_path / "canary-admission.json",
            clock=lambda: _NOW,
        )


def test_revoke_canary_pairing_is_append_only_and_blocks_future_admission(tmp_path: Path) -> None:
    ledger_path = tmp_path / "canary-admission.json"
    plan = _ema_plan(ledger_path)
    activation = apply_canary_activation(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        ledger_path=ledger_path,
        clock=lambda: _NOW + 1,
    )

    revocation = revoke_canary_pairing(
        program_key="ema_crossover_signal",
        account_id="paper-account",
        actor="local:test-operator",
        reason="End the canary before any subsequent Start or Resume.",
        ledger_path=ledger_path,
        clock=lambda: _NOW + 2,
    )

    assert revocation.action == "revoked"
    assert revocation.sequence == activation.sequence + 1
    assert revocation.previous_event_hash == activation.event_hash
    assert active_canary_pairings(ledger_path=ledger_path) == frozenset()
    assert canary_pairing_admitted(
        program_key="ema_crossover_signal",
        account_id="paper-account",
        ledger_path=ledger_path,
    ) is False
    raw = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert [event["action"] for event in raw["events"]] == ["activated", "revoked"]


def test_valid_prefix_rollback_fails_closed_against_monotonic_checkpoint(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "canary-admission.json"
    plan = _ema_plan(ledger_path)
    apply_canary_activation(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        ledger_path=ledger_path,
        clock=lambda: _NOW + 1,
    )
    activation_only_ledger = ledger_path.read_bytes()
    revoke_canary_pairing(
        program_key="ema_crossover_signal",
        account_id="paper-account",
        actor="local:test-operator",
        reason="End the canary before any subsequent Start or Resume.",
        ledger_path=ledger_path,
        clock=lambda: _NOW + 2,
    )

    ledger_path.write_bytes(activation_only_ledger)

    with pytest.raises(CanaryAdmissionLedgerError, match="monotonic checkpoint"):
        active_canary_pairings(ledger_path=ledger_path)
    assert canary_pairing_admitted(
        program_key="ema_crossover_signal",
        account_id="paper-account",
        ledger_path=ledger_path,
    ) is False


def test_canary_pairing_admission_fails_closed_on_a_tampered_ledger(tmp_path: Path) -> None:
    ledger_path = tmp_path / "canary-admission.json"
    plan = _ema_plan(ledger_path)
    apply_canary_activation(
        plan=plan,
        confirmation_token=plan.confirmation_token,
        ledger_path=ledger_path,
        clock=lambda: _NOW + 1,
    )
    raw = json.loads(ledger_path.read_text(encoding="utf-8"))
    raw["events"][0]["reason"] = "Tampered after activation."
    ledger_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CanaryAdmissionLedgerError, match="invalid"):
        active_canary_pairings(ledger_path=ledger_path)
    assert canary_pairing_admitted(
        program_key="ema_crossover_signal",
        account_id="paper-account",
        ledger_path=ledger_path,
    ) is False


def _ema_plan(ledger_path: Path) -> CanaryActivationPlan:
    return plan_canary_activation(
        program_key="ema_crossover_signal",
        account_id="paper-account",
        actor="local:test-operator",
        reason="Begin the reviewed one-share EMA paper canary.",
        ledger_path=ledger_path,
        confirmation_ttl_ms=120_000,
        clock=lambda: _NOW,
    )


def test_canary_rollback_admitted_when_flat() -> None:
    decision = evaluate_canary_rollback(
        strategy_instance_id="sid-1",
        stop_outcome="STOPPED_FLAT",
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is True
    assert decision.reason_code == "CANARY_ROLLBACK_ADMITTED"
    assert isinstance(decision, CanaryRollbackDecision)


def test_canary_rollback_admitted_with_an_approved_carried_exposure() -> None:
    decision = evaluate_canary_rollback(
        strategy_instance_id="sid-1",
        stop_outcome="STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE",
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is True
    assert decision.reason_code == "CANARY_ROLLBACK_ADMITTED"


def test_canary_rollback_refused_when_flatten_is_required() -> None:
    """#1729 AC10: rollback stops the canary ONLY at a Clerk-proved safe
    boundary -- unapproved attributed exposure is not one."""
    decision = evaluate_canary_rollback(
        strategy_instance_id="sid-1",
        stop_outcome="STOP_REQUIRES_FLATTEN",
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is False
    assert decision.reason_code == "CANARY_ROLLBACK_REQUIRES_FLATTEN"
    assert decision.next_step is not None


def test_canary_rollback_refused_when_custody_is_unprovable() -> None:
    """#1729 AC10: an unprovable boundary is refused outright, unlike an
    ordinary Stop, which records `STOPPED_CUSTODY_UNPROVABLE` honestly and
    still proceeds -- rollback is the stricter of the two."""
    decision = evaluate_canary_rollback(
        strategy_instance_id="sid-1",
        stop_outcome="STOPPED_CUSTODY_UNPROVABLE",
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is False
    assert decision.reason_code == "CANARY_ROLLBACK_BOUNDARY_UNPROVABLE"


def test_canary_rollback_decision_carries_the_exact_stop_outcome_it_classified() -> None:
    decision = evaluate_canary_rollback(
        strategy_instance_id="sid-1",
        stop_outcome="STOP_REQUIRES_FLATTEN",
        evaluated_at_ms=_NOW,
    )

    assert decision.stop_outcome == "STOP_REQUIRES_FLATTEN"
    assert decision.strategy_instance_id == "sid-1"
    assert decision.evaluated_at_ms == _NOW
