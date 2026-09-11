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


def test_an_accepted_current_review_applies_only_to_its_exact_configuration() -> None:
    dossier = _dossier()

    receipt = service.assess(dossier, _proposed(dossier))
    changed = service.assess(dossier, {**_proposed(dossier), "symbol": "MSFT"})

    assert receipt.applicable is True
    assert receipt.classification == "reviewed_deviations"
    assert "independent Paper or Live safety gates" in receipt.explanation
    assert changed.applicable is False
    assert changed.mismatched_fields == ("symbol",)


def test_changed_engine_evidence_requires_a_new_review_before_applicability() -> None:
    dossier = _dossier(evidence_revision="b" * 64)

    receipt = service.assess(dossier, _proposed(dossier))

    assert receipt.applicable is False
    assert receipt.mismatched_fields == ()
    assert "evidence changed" in receipt.explanation.lower()


def test_historical_null_program_version_requires_an_explicit_reviewed_version() -> None:
    dossier = _dossier(program_version=None, authorized_program_version="ema-v1")

    receipt = service.assess(dossier, _proposed(_dossier()))

    assert receipt.applicable is True


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
