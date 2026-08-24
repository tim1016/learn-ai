"""Admission-time regression tests for strategy validation evidence."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.strategy_validation_admission as validation_admission
from app.schemas.strategy_validation import (
    StrategyBehavioralEquivalence,
    StrategyEvidenceSnapshot,
    StrategyValidationDiagnostics,
    StrategyValidationEntry,
    StrategyValidationFlagEvent,
)
from app.services.strategy_validation_manifest import StrategyValidationManifestError

_NOW = 1_700_000_000_000
_SHA = "a" * 64


def _entry(*, verdict: str = "accepted_for_deploy") -> StrategyValidationEntry:
    diagnostics = StrategyValidationDiagnostics(
        verdict="passed",
        trades_matched=2,
        trades_validated=2,
        pnl_max_abs_diff="0.00",
    )
    snapshot = StrategyEvidenceSnapshot(
        validator_code_ref="PythonDataService/validator.py",
        validator_code_sha256=_SHA,
        settings_file_ref="PythonDataService/settings.json",
        settings_file_sha256=_SHA,
        qc_cloud_backtest_id="qc-accepted",
        audit_copy_ref="references/audit.py",
        audit_copy_sha256=_SHA,
        reconciliation_ref="docs/references/reconciliation.md",
        validation_case_symbol="SPY",
        reconciliation_status="passed",
        diagnostics=diagnostics,
    )
    event = StrategyValidationFlagEvent(
        event_id="event-accepted",
        strategy_key="ema_crossover_signal",
        flag="validated",
        flagged_by="test",
        flagged_at_ms=_NOW - 1,
        reason="Test validation receipt.",
        behavioral_equivalence=StrategyBehavioralEquivalence(
            verdict=verdict,
            detail="Test proof.",
        ),
        evidence_snapshot=snapshot,
        evidence_snapshot_sha256=_SHA,
    )
    return StrategyValidationEntry(
        strategy_key="ema_crossover_signal",
        display_name="EMA",
        description="test",
        validation_state="validated",
        deployable=verdict == "accepted_for_deploy",
        validator_code_ref=snapshot.validator_code_ref,
        validator_code_sha256=snapshot.validator_code_sha256,
        settings_file_ref=snapshot.settings_file_ref,
        settings_file_sha256=snapshot.settings_file_sha256,
        qc_cloud_backtest_id=snapshot.qc_cloud_backtest_id,
        audit_copy_ref=snapshot.audit_copy_ref,
        audit_copy_sha256=snapshot.audit_copy_sha256,
        reconciliation_ref=snapshot.reconciliation_ref,
        validation_case_symbol=snapshot.validation_case_symbol,
        reconciliation_status=snapshot.reconciliation_status,
        diagnostics=diagnostics,
        behavioral_equivalence=event.behavioral_equivalence,
        current_flag_event=event,
    )


@pytest.fixture(autouse=True)
def _current_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation_admission, "strategy_settings_file_is_current", lambda _entry: True)
    monkeypatch.setattr(validation_admission, "strategy_validator_code_is_current", lambda _entry: True)
    monkeypatch.setattr(validation_admission, "strategy_audit_copy_is_current", lambda _entry: True)


def test_current_validation_fact_rehashes_an_accepted_proof_at_start() -> None:
    fact = validation_admission.current_strategy_validation_fact(
        SimpleNamespace(strategy_key="ema_crossover_signal", evidence_override=None),
        _NOW,
        entries_loader=lambda: [_entry()],
    )

    assert fact.state == "VERIFIED"
    assert fact.evidence_status == "accepted"
    assert fact.event_id == "event-accepted"
    assert "strategy-validation:snapshot:" + _SHA in fact.evidence_refs


def test_current_validation_fact_blocks_when_a_rehashed_artifact_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validation_admission, "strategy_audit_copy_is_current", lambda _entry: False)

    fact = validation_admission.current_strategy_validation_fact(
        SimpleNamespace(strategy_key="ema_crossover_signal", evidence_override=None),
        _NOW,
        entries_loader=lambda: [_entry()],
    )

    assert fact.state == "UNVERIFIED"
    assert fact.evidence_status == "accepted"


def test_current_validation_fact_requires_override_for_current_evidence_only_proof() -> None:
    entry = _entry(verdict="evidence_only")

    without_override = validation_admission.current_strategy_validation_fact(
        SimpleNamespace(strategy_key="ema_crossover_signal", evidence_override=None),
        _NOW,
        entries_loader=lambda: [entry],
    )
    with_override = validation_admission.current_strategy_validation_fact(
        SimpleNamespace(strategy_key="ema_crossover_signal", evidence_override=object()),
        _NOW,
        entries_loader=lambda: [entry],
    )

    assert without_override.state == "UNVERIFIED"
    assert with_override.state == "VERIFIED"
    assert with_override.evidence_status == "evidence_only"


def _proofless_evidence_only_entry() -> StrategyValidationEntry:
    """A production candidate flagged by a human with no registered proof artifacts at all."""
    base = _entry(verdict="evidence_only")
    assert base.current_flag_event is not None
    event = base.current_flag_event.model_copy(update={"evidence_snapshot": StrategyEvidenceSnapshot()})
    return base.model_copy(
        update={
            "validator_code_ref": None,
            "validator_code_sha256": None,
            "settings_file_ref": None,
            "settings_file_sha256": None,
            "qc_cloud_backtest_id": None,
            "audit_copy_ref": None,
            "audit_copy_sha256": None,
            "reconciliation_ref": None,
            "validation_case_symbol": None,
            "reconciliation_status": None,
            "diagnostics": None,
            "current_flag_event": event,
        }
    )


def test_override_accepts_absent_artifacts_but_never_drifted_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator decision 2026-08-24: the durable evidence-only override accepts
    the *absence* of registered reference artifacts (there is nothing to be
    current against), but a *recorded* artifact whose bytes drifted still
    refuses — the override is a risk acceptance, not a freshness bypass."""
    # The real checks return False for a None ref; mirror that here so the
    # absent-artifact branch is proven to consult the ref, not the check.
    monkeypatch.setattr(validation_admission, "strategy_settings_file_is_current", lambda _e: False)
    monkeypatch.setattr(validation_admission, "strategy_validator_code_is_current", lambda _e: False)
    monkeypatch.setattr(validation_admission, "strategy_audit_copy_is_current", lambda _e: False)

    absent = validation_admission.current_strategy_validation_fact(
        SimpleNamespace(strategy_key="ema_crossover_signal", evidence_override=object()),
        _NOW,
        entries_loader=lambda: [_proofless_evidence_only_entry()],
    )
    drifted = validation_admission.current_strategy_validation_fact(
        SimpleNamespace(strategy_key="ema_crossover_signal", evidence_override=object()),
        _NOW,
        entries_loader=lambda: [_entry(verdict="evidence_only")],
    )

    assert absent.state == "VERIFIED"
    assert absent.evidence_status == "evidence_only"
    assert drifted.state == "UNVERIFIED"


def test_current_validation_fact_fails_closed_when_the_event_receipt_is_unreadable() -> None:
    def unreadable_entries() -> list[StrategyValidationEntry]:
        raise StrategyValidationManifestError("snapshot SHA mismatch")

    fact = validation_admission.current_strategy_validation_fact(
        SimpleNamespace(strategy_key="ema_crossover_signal", evidence_override=None),
        _NOW,
        entries_loader=unreadable_entries,
    )

    assert fact.state == "UNREADABLE"
    assert fact.evidence_status == "unknown"
