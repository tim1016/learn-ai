"""Exact Golden Validation applicability is a pure, reusable admission receipt."""

from __future__ import annotations

import json

from app.research.golden_validation import repository, service


def _dossier(
    *,
    evidence_revision: str = "a" * 64,
    reviewed_revision: str = "a" * 64,
    program_version: str | None = "ema-v1",
    authorized_program_version: str | None = None,
):
    validation_case = {
        "schema_version": 1,
        "source_run_id": 42,
        "strategy": {"name": "ema_crossover_signal", "program_version": program_version},
        "symbol": "AAPL",
        "parameters": {"symbol": "AAPL", "gap": 0.2, "rsi_min": 50, "rsi_max": 70},
        "window": {"start_ms": 1, "end_ms": 2, "timespan": "minute"},
        "data_policy": {"fixture_id": "fixture", "fixture_sha256": "f" * 64},
        "execution": {
            "fill_mode": "signal_bar_close",
            "initial_cash": 100_000.0,
            "configuration": {"compatibility_profile": "us-equity-raw-ibkr-v1"},
        },
        "requested_engine": "both",
        "parity_group_id": "pg-42",
    }
    golden = repository.GoldenRunRow(
        id=7,
        source_run_id=42,
        command_id="designate-42",
        command_sha256="c" * 64,
        label="AAPL baseline",
        strategy_name="ema_crossover_signal",
        symbol="AAPL",
        validation_case_json=json.dumps(validation_case),
        case_sha256="d" * 64,
        rationale="Chosen after research.",
        designated_by="local:reviewer",
        designated_at_ms=10,
    )
    review = repository.GoldenReviewRow(
        id=9,
        golden_run_id=7,
        command_id="review-42",
        command_sha256="e" * 64,
        expected_evidence_revision=reviewed_revision,
        decision="accept",
        classification="manual_override" if program_version is None else "reviewed_deviations",
        evidence_state="deviations",
        parity_verdict_id=3,
        evidence_json='{"computed_state":"deviations"}',
        evidence_sha256="f" * 64,
        reason="One extra closing trade is understood.",
        quantconnect_backtest_id=None,
        authorized_program_version=authorized_program_version,
        reviewed_by="local:reviewer",
        reviewed_at_ms=11,
    )
    evidence = service.EvidenceView(
        state="deviations",
        revision=evidence_revision,
        parity_verdict_id=3,
        payload={"computed_state": "deviations"},
    )
    return service.GoldenValidationDossier(golden, validation_case, evidence, (review,))


def _proposed(dossier: service.GoldenValidationDossier) -> dict:
    case = dossier.validation_case
    return {
        "strategy_name": case["strategy"]["name"],
        "program_version": case["strategy"]["program_version"],
        "symbol": case["symbol"],
        "parameters": case["parameters"],
        "window": case["window"],
        "data_policy": case["data_policy"],
        "execution": case["execution"],
    }


def test_a_historical_v2_agreement_is_not_promoted_to_certificate_grade_agreement() -> None:
    dossier = _dossier()
    verdict = {
        "id": 3,
        "parity_group_id": "pg-42",
        "left_run_id": 42,
        "right_run_id": 43,
        "verdict_version": 2,
        "status": "agree",
        "verdict_json": '{"schema_version":2,"status":"agree"}',
        "created_at_ms": 10,
    }

    evidence = service._evidence_for(dossier.golden_run, verdict)

    assert evidence.state == "corrupt"
    assert evidence.payload["parity_verdict"]["status"] == "agree"
    assert "parity_verdict_not_certificate_grade" in evidence.payload["qualification_warnings"]


def test_a_v3_verdict_for_a_different_source_run_is_corrupt_evidence() -> None:
    dossier = _dossier()
    verdict = {
        "id": 3,
        "parity_group_id": "pg-42",
        "left_run_id": 999,
        "right_run_id": 43,
        "verdict_version": 3,
        "status": "agree",
        "verdict_json": '{"schema_version":3,"status":"agree"}',
        "created_at_ms": 10,
    }

    evidence = service._evidence_for(dossier.golden_run, verdict)

    assert evidence.state == "corrupt"
    assert evidence.payload["parity_verdict"]["status"] == "agree"
    assert "parity_verdict_not_certificate_grade" in evidence.payload["qualification_warnings"]


def test_an_incomplete_v3_payload_cannot_claim_engine_agreement() -> None:
    dossier = _dossier()
    verdict = {
        "id": 3,
        "parity_group_id": "pg-42",
        "left_run_id": 42,
        "right_run_id": 43,
        "verdict_version": 3,
        "status": "agree",
        "verdict_json": '{"schema_version":3,"status":"agree"}',
        "created_at_ms": 10,
    }

    evidence = service._evidence_for(dossier.golden_run, verdict)

    assert evidence.state == "corrupt"
    assert "parity_verdict_not_certificate_grade" in evidence.payload["qualification_warnings"]


def test_a_v3_match_with_empty_receipt_bodies_cannot_claim_engine_agreement() -> None:
    dossier = _dossier()
    payload = {
        "schema_version": 3,
        "parity_group_id": "pg-42",
        "left_execution_id": 42,
        "right_execution_id": 43,
        "engines": {"left": "python", "right": "lean"},
        "status": "agree",
        "reason": None,
        "tolerances": {"fill_price_atol": "0.01"},
        "native_metric_parity": {"status": "match"},
        "readiness_parity": {"status": "match"},
        "input_parity": {
            "status": "match",
            "compared_field_count": 1,
            "fixture_id": "fixture",
            "fixture_sha256": "f" * 64,
            "mismatched_fields": [],
        },
        "parameter_parity": {"status": "match", "compared_field_count": 1, "mismatched_fields": []},
        "program_version_parity": {
            "status": "match",
            "left_program_version": "ema-v1",
            "right_program_version": "ema-v1",
        },
        "divergences": [],
        "counts_by_category": {},
        "computed_at_ms": 10,
    }
    verdict = {
        "id": 3,
        "parity_group_id": "pg-42",
        "left_run_id": 42,
        "right_run_id": 43,
        "verdict_version": 3,
        "status": "agree",
        "verdict_json": json.dumps(payload),
        "created_at_ms": 10,
    }

    evidence = service._evidence_for(dossier.golden_run, verdict)

    assert evidence.state == "corrupt"
    assert "parity_verdict_not_certificate_grade" in evidence.payload["qualification_warnings"]


def test_positively_affected_accepted_case_requires_a_new_explicit_review() -> None:
    from dataclasses import replace

    from app.research.backtest_runs.evidence_provenance import assess_evidence_provenance

    dossier = _dossier()
    case = {**dossier.validation_case, "evidence_provenance": {
        "schema_version": 1,
        "data_contract": "lake_complete_sessions/v1",
        "statistics_basis": "paired_realized_trade_ledger/v1",
        "daily_return_convention": "platform_skip_first_session/v1",
    }}
    affected = replace(dossier, validation_case=case)
    assert assess_evidence_provenance(case["evidence_provenance"]).affected_issues == ("#2447", "#2448")
    receipt = service.assess_deployment_scope(affected, _proposed(dossier))
    assert not receipt.applicable
    assert "#2447" in receipt.explanation
    assert dossier.latest_review.reason == affected.latest_review.reason
    assert dossier.latest_review.expected_evidence_revision == affected.latest_review.expected_evidence_revision

    override = replace(affected.latest_review, classification="manual_override", evidence_json='{"acknowledge_provenance_risk":true}')
    approved = replace(affected, reviews=(override,))
    assert service.assess_deployment_scope(approved, _proposed(dossier)).applicable


def test_unknown_conventions_are_not_known_affected_and_preserve_existing_acceptance() -> None:
    dossier = _dossier()
    assert dossier.evidence_applicability.status == "unknown"
    assert dossier.evidence_applicability.affected_issues == ()
    assert service.assess_deployment_scope(dossier, _proposed(dossier)).applicable


def test_current_recorded_conventions_preserve_the_review_revision() -> None:
    from dataclasses import replace
    dossier = _dossier()
    case = {**dossier.validation_case, "evidence_provenance": {
        "schema_version": 1, "data_contract": "fixture_identity/v1",
        "statistics_basis": "marked_equity_curve/v1",
        "daily_return_convention": "initial_capital_first_session/v1",
        "closing_bar_convention": "skip_closing_bar/v1",
    }}
    current = replace(dossier, validation_case=case)
    assert current.evidence_applicability.status == "current"
    assert current.review_is_current is True
    assert current.evidence.revision == dossier.evidence.revision
