"""Admission-time regression tests for strategy validation evidence."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.strategy_validation_admission as validation_admission
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.golden_validation.repository import GoldenReviewRow, GoldenRunRow
from app.research.golden_validation.service import (
    EvidenceView,
    GoldenDeploymentCandidates,
    GoldenValidationDossier,
)
from app.schemas.run_admission import StrategyValidationAdmissionFact
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
    # The override is the same durable record on every world, so its next
    # step names no mode: "the paper-mode evidence override" was a lying
    # label once the live world could reach this refusal (slice 7).
    assert without_override.next_step == "Record the evidence override before deploying."
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


def _legacy_fact() -> StrategyValidationAdmissionFact:
    return StrategyValidationAdmissionFact(
        state="VERIFIED",
        strategy_key="ema_crossover_signal",
        evidence_status="accepted",
        event_id="legacy-event",
        evidence_snapshot_sha256=_SHA,
        verified_at_ms=_NOW,
        explanation="Legacy proof.",
    )


def _golden_dossier(
    *,
    golden_id: int = 41,
    decision: str = "accept",
    current: bool = True,
) -> GoldenValidationDossier:
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    assert registration.signal_program_contract is not None
    revision = "b" * 64
    reviewed_revision = revision if current else "c" * 64
    run = GoldenRunRow(
        id=golden_id,
        source_run_id=17,
        command_id=f"designate-{golden_id}",
        command_sha256="d" * 64,
        label="SPY research choice",
        strategy_name="ema_crossover_signal",
        symbol="SPY",
        validation_case_json="{}",
        case_sha256="e" * 64,
        rationale="Chosen after parameter research.",
        designated_by="local:researcher",
        designated_at_ms=_NOW - 10,
    )
    review = GoldenReviewRow(
        id=golden_id + 100,
        golden_run_id=golden_id,
        command_id=f"review-{golden_id}",
        command_sha256="f" * 64,
        expected_evidence_revision=reviewed_revision,
        decision=decision,
        classification="engine_agreement" if decision == "accept" else None,
        evidence_state="agreement",
        parity_verdict_id=9,
        evidence_json="{}",
        evidence_sha256="1" * 64,
        reason="Reviewed evidence.",
        quantconnect_backtest_id=None,
        authorized_program_version=None,
        reviewed_by="local:reviewer",
        reviewed_at_ms=_NOW - 5,
    )
    validation_case = {
        "strategy": {
            "name": "ema_crossover_signal",
            "program_version": registration.signal_program_contract.program_version,
        },
        "symbol": "SPY",
        "parameters": registration.param_schema(symbol="SPY").model_dump(mode="json"),
    }
    return GoldenValidationDossier(
        golden_run=run,
        validation_case=validation_case,
        evidence=EvidenceView(
            state="agreement",
            revision=revision,
            parity_verdict_id=9,
            payload={},
        ),
        reviews=(review,),
    )


def _fresh_binding(**updates: object) -> SimpleNamespace:
    values = {
        "strategy_key": "ema_crossover_signal",
        "symbol": "SPY",
        "strategy_params": {},
        "sealed_program": None,
        "evidence_override": None,
    }
    values.update(updates)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_deployment_fact_accepts_exact_golden_scope_and_resolves_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validation_admission, "current_strategy_validation_fact", lambda *_args: _legacy_fact())

    async def load(
        strategy_key: str,
        symbol: str,
        parameters: dict[str, object],
    ) -> GoldenDeploymentCandidates:
        registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
        assert strategy_key == "ema_crossover_signal"
        assert symbol == "SPY"
        assert parameters == registration.param_schema(symbol="SPY").model_dump(mode="json")
        # The repository lookup is exact and unpaginated, so this deliberately
        # old record remains visible regardless of newer research history.
        return GoldenDeploymentCandidates((_golden_dossier(golden_id=1),), True)

    fact = await validation_admission.current_deployment_strategy_validation_fact(
        _fresh_binding(),
        _NOW,
        golden_loader=load,
    )

    assert fact.state == "VERIFIED"
    assert fact.event_id == "golden-validation:1:review:101"
    assert "golden-validation:classification:engine_agreement" in fact.evidence_refs


@pytest.mark.asyncio
async def test_deployment_fact_refuses_parameter_drift_once_strategy_has_golden_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validation_admission, "current_strategy_validation_fact", lambda *_args: _legacy_fact())

    async def load(
        _strategy_key: str,
        _symbol: str,
        _parameters: dict[str, object],
    ) -> GoldenDeploymentCandidates:
        return GoldenDeploymentCandidates((), True)

    fact = await validation_admission.current_deployment_strategy_validation_fact(
        _fresh_binding(strategy_params={"gap": 0.75}),
        _NOW,
        golden_loader=load,
    )

    assert fact.state == "UNVERIFIED"
    assert "resolved parameter set" in fact.explanation


@pytest.mark.asyncio
async def test_deployment_fact_preserves_v1_until_strategy_has_a_golden_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validation_admission, "current_strategy_validation_fact", lambda *_args: _legacy_fact())

    async def load(
        _strategy_key: str,
        _symbol: str,
        _parameters: dict[str, object],
    ) -> GoldenDeploymentCandidates:
        return GoldenDeploymentCandidates((), False)

    fact = await validation_admission.current_deployment_strategy_validation_fact(
        _fresh_binding(),
        _NOW,
        golden_loader=load,
    )

    assert fact.event_id == "legacy-event"


@pytest.mark.asyncio
async def test_resume_pinned_to_rejected_golden_record_never_falls_back_to_newer_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validation_admission, "current_strategy_validation_fact", lambda *_args: _legacy_fact())
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    assert registration.signal_program_contract is not None
    parameters = registration.param_schema(symbol="SPY").model_dump(mode="json")
    sealed = SimpleNamespace(
        validation_event_id="golden-validation:40:review:140",
        configured_signal=SimpleNamespace(
            program_key="ema_crossover_signal",
            program_version=registration.signal_program_contract.program_version,
            data=SimpleNamespace(symbol="SPY"),
            parameters={name: SimpleNamespace(value=value) for name, value in parameters.items()},
        ),
    )

    async def load(
        _strategy_key: str,
        _symbol: str,
        _parameters: dict[str, object],
    ) -> GoldenDeploymentCandidates:
        return GoldenDeploymentCandidates(
            (_golden_dossier(golden_id=41), _golden_dossier(golden_id=40, decision="reject")),
            True,
        )

    async def load_one(golden_validation_id: int) -> GoldenValidationDossier | None:
        assert golden_validation_id == 40
        return _golden_dossier(golden_id=40, decision="reject")

    fact = await validation_admission.current_deployment_strategy_validation_fact(
        _fresh_binding(sealed_program=sealed),
        _NOW,
        golden_loader=load,
        golden_by_id_loader=load_one,
    )

    assert fact.state == "UNVERIFIED"
    assert "rejected" in fact.explanation


@pytest.mark.asyncio
async def test_resume_loads_its_pinned_golden_record_without_catalog_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validation_admission, "current_strategy_validation_fact", lambda *_args: _legacy_fact())
    registration = _STRATEGY_REGISTRY["ema_crossover_signal"]
    assert registration.signal_program_contract is not None
    parameters = registration.param_schema(symbol="SPY").model_dump(mode="json")
    sealed = SimpleNamespace(
        validation_event_id="golden-validation:1:review:101",
        configured_signal=SimpleNamespace(
            program_key="ema_crossover_signal",
            program_version=registration.signal_program_contract.program_version,
            data=SimpleNamespace(symbol="SPY"),
            parameters={name: SimpleNamespace(value=value) for name, value in parameters.items()},
        ),
    )

    async def paged_loader(
        _strategy_key: str,
        _symbol: str,
        _parameters: dict[str, object],
    ) -> GoldenDeploymentCandidates:
        raise AssertionError("Pinned Resume must not search a paginated catalog")

    async def load_one(golden_validation_id: int) -> GoldenValidationDossier | None:
        assert golden_validation_id == 1
        return _golden_dossier(golden_id=1)

    fact = await validation_admission.current_deployment_strategy_validation_fact(
        _fresh_binding(sealed_program=sealed),
        _NOW,
        golden_loader=paged_loader,
        golden_by_id_loader=load_one,
    )

    assert fact.state == "VERIFIED"
    assert fact.event_id == "golden-validation:1:review:101"
