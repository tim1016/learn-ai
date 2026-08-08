"""Contract tests for the synthetic-only Alpaca Clerk qualification report."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.account_custody_synthetic import (
    SyntheticAbortCause as DomainSyntheticAbortCause,
)
from app.models.account_custody_synthetic import (
    SyntheticScenarioCategory as DomainSyntheticScenarioCategory,
)
from app.models.account_custody_synthetic import SyntheticScenarioId
from app.schemas.account_custody_qualification import (
    SyntheticAbortCause,
    SyntheticCustodyRehearsalReport,
    SyntheticEvidenceWorksheet,
    SyntheticHarnessAbortReport,
    SyntheticScenarioResult,
    account_custody_qualification_payload_sha256,
    synthetic_abort_cause_for_scenario,
    synthetic_abort_class_for_cause,
)
from app.services.account_custody_synthetic_scenarios import (
    SYNTHETIC_REHEARSAL_SCENARIO_IDS as REGISTRY_SCENARIO_IDS,
)
from app.services.account_custody_synthetic_scenarios import (
    SYNTHETIC_REHEARSAL_SCENARIOS,
    SyntheticScenarioCategory,
)
from app.services.account_custody_synthetic_scenarios import (
    SyntheticAbortCause as RegistrySyntheticAbortCause,
)


def _broker_proof(observed_at_ms: int) -> dict[str, Any]:
    return {
        "proof_kind": "SIMULATED_BROKER_SNAPSHOT",
        "proof_reference": "simulated-broker:flat",
        "observed_at_ms": observed_at_ms,
        "positions": {},
        "open_order_ids": [],
        "orders": [],
        "submit_calls": [],
        "cancel_calls": [],
        "lookup_calls": [],
    }


def _worksheet_payload(*, source_event_at_ms: int = 1_000) -> dict[str, Any]:
    return {
        "evidence_scope": "DURABLE_CLERK_TRANSITION",
        "authority_participation": "READ_WRITE",
        "authority_generation": 2,
        "db_identity_token": "qualification-db-token",
        "control_revision": 17,
        "command_ids": ["command-1415"],
        "effect_operation_ids": ["effect-1415"],
        "order_refs": ["order-ref-1415"],
        "broker_order_ids": ["broker-order-1415"],
        "receipt_ids": ["receipt-1415"],
        "source_event_at_ms": source_event_at_ms,
        "clerk_observed_at_ms": source_event_at_ms + 1,
        "recorded_at_ms": source_event_at_ms + 2,
        "expected_state": "Custody remains fail-closed.",
        "observed_state": "Custody remained fail-closed.",
        "operator_action": "Recorded synthetic evidence without contacting a live broker.",
        "warning_error_log_refs": ["qualification.log#scenario"],
        "broker_before": _broker_proof(source_event_at_ms),
        "broker_after": _broker_proof(source_event_at_ms + 2),
    }


def _scenario_payload(scenario_id: str, index: int) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "name": scenario_id.replace("_", " "),
        "status": "REHEARSED_PENDING_LIVE_CONFIRMATION",
        "abort_class": "NONE",
        "abort_cause": "NONE",
        "evidence_refs": [f"pytest:{scenario_id}"],
        "worksheet": _worksheet_payload(source_event_at_ms=1_000 + index * 10),
        "limitations": [],
        "fault_seam_required": False,
        "fault_seam_exercised": False,
        "failure_detail": None,
    }


def _ui_correlation_records() -> list[dict[str, Any]]:
    revisions = (41, 41, 42, 42, 43)
    return [
        {
            "page_load_index": page_load_index,
            "event_index": event_index,
            "browser_epoch": f"browser-reload-{page_load_index}",
            "revision": revision,
            "historical_snapshot_state": "OFF_DUTY_STOPPED_FLAT_POST_RESTART",
            "presented_action_id": "resume",
            "click_target": "BROKER_ACK_RAW_EVIDENCE",
            "evidence_request_method": "GET",
            "evidence_request_path": (
                "/api/brokers/alpaca/accounts/DUM284968/bots/sid-001/evidence"
                "?transaction_ref=tx-001&client_hint=operator-transaction-timeline"
            ),
            "operation_reference": f"receipt-{page_load_index}-{event_index}",
            "proof_reference": f"receipt-{page_load_index}-{event_index}",
            "dispatched_lifecycle_action_id": None,
            "durable_lifecycle_receipt_id": None,
        }
        for page_load_index in range(5)
        for event_index, revision in enumerate(revisions)
    ]


def _seal(payload: dict[str, Any]) -> dict[str, Any]:
    payload.pop("report_sha256", None)
    payload["report_sha256"] = account_custody_qualification_payload_sha256(payload)
    return payload


def _report_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_ms": 1_800_000_000_000,
        "label": "PRE_LIVE_REHEARSAL_SYNTHETIC",
        "live_environment_status": "NOT_RUN",
        "adr_0035_status": "PROPOSED",
        "overall_status": "REHEARSED_PENDING_LIVE_CONFIRMATION",
        "abort_class": "NONE",
        "protected_generation": {
            "artifacts_root": "/app/artifacts/pre-live-rehearsal",
            "account_id": "QUAL-1415",
            "authority_generation": 2,
            "db_identity_token": "qualification-db-token",
            "filesystem_type": "xfs",
            "wal_storage_guard_checked": True,
            "production_account_id": "PRODUCTION",
            "production_authority_root": "/app/production-alpaca-clerk",
            "production_mount_access": "READ_ONLY",
            "qualification_mount_access": "READ_WRITE",
            "production_mount_id": 12,
            "qualification_mount_id": 13,
            "mount_identity_separated": True,
            "production_generation_opened_read_write": False,
            "diagnostic_read_boundary": "ONE_SHOT_SERVICE_CONTAINER",
        },
        "polygon_replay": {
            "fixture_ref": "tests/fixtures/polygon_capture/spy_minute_2025-01-13_2025-01-17",
            "fixture_sha256": "a" * 64,
            "fixture_bar_count": 4_062,
            "replayed_minute_count": 3,
            "synthesized_5s_count": 36,
            "emitted_minute_count": 3,
            "decision_count": 3,
            "decision_outcomes": ["HOLD", "ENTER", "HOLD"],
            "timestamp_contract": "int64_ms_utc",
            "decision_consumer_path": ("strategy_evaluations/DeploymentValidationDecisionKernel"),
            "aggregation_path": ("stream_minute_bars/aggregate_realtime_bar/IbkrMarketDataFeed"),
        },
        "recovery": {
            "backup_manifest_ref": "backup/manifest.json",
            "backup_sha256": "b" * 64,
            "restore_receipt_ref": "restore/receipt.json",
            "rebuild_receipt_ref": "rebuild/receipt.json",
            "integrity_check": "ok",
            "hash_head_before": "head-1415",
            "hash_head_after_restore": "head-1415",
            "hash_head_after_rebuild": "head-1415",
        },
        "ui_correlation": {
            "campaign_id": "frontend-browser-network-correlation-1413",
            "test_ref": "Frontend/tests/e2e/alpaca-clerk-ui-correlation.spec.ts::campaign",
            "command": ["npx", "playwright", "test"],
            "git_commit_sha": "1" * 40,
            "test_source_sha256": "2" * 64,
            "campaign_contract_sha256": "7" * 64,
            "executed_at_ms": 1_800_000_000_000,
            "duration_ms": 1_000,
            "stdout_sha256": "3" * 64,
            "stderr_sha256": "4" * 64,
            "evidence_receipt_sha256": "5" * 64,
            "interaction_count": 25,
            "page_load_count": 5,
            "browser_reload_count": 4,
            "evidence_request_count": 25,
            "evidence_response_proof_reference_count": 25,
            "native_event_source_connection_count": 25,
            "observed_revision_count": 25,
            "revision_sequence": [41, 41, 42, 42, 43],
            "historical_snapshot_state": "OFF_DUTY_STOPPED_FLAT_POST_RESTART",
            "presented_action_id": "resume",
            "correlation_records": _ui_correlation_records(),
            "lifecycle_request_count": 0,
            "lifecycle_action_ids": [],
            "browser_reload_coverage": ("PLAYWRIGHT_NATIVE_EVENT_SOURCE_PAGE_RELOAD_CAMPAIGN"),
            "durable_lifecycle_receipt_ids": [],
            "disposition": "HISTORICAL_TRIGGER_NOT_REPRODUCED_BROWSER_CAMPAIGN",
        },
        "scenarios": [_scenario_payload(scenario_id, index) for index, scenario_id in enumerate(REGISTRY_SCENARIO_IDS)],
    }
    return _seal(payload)


def _abort_scenario(
    payload: dict[str, Any],
    *,
    scenario_id: str,
    abort_class: str,
) -> None:
    scenario = next(row for row in payload["scenarios"] if row["scenario_id"] == scenario_id)
    scenario["status"] = "ABORTED"
    scenario["abort_class"] = abort_class
    scenario["abort_cause"] = synthetic_abort_cause_for_scenario(scenario["scenario_id"])
    scenario["failure_detail"] = f"{scenario_id} deliberately failed"
    payload["overall_status"] = "ABORTED"
    payload["abort_class"] = abort_class
    _seal(payload)


def test_synthetic_report_accepts_only_complete_canonical_scenario_order() -> None:
    report = SyntheticCustodyRehearsalReport.model_validate(_report_payload())

    assert tuple(row.scenario_id for row in report.scenarios) == (
        "polygon_replay_closed_minutes",
        "feed_one_print_stall",
        "feed_never_first_bar",
        "lost_submit_exact_identity",
        "duplicate_trade_update",
        "cancel_fill_race",
        "lost_cancel_retains_custody",
        "trade_update_gap_reconciliation",
        "bot_uncertainty_isolation",
        "unclassified_uncertainty_account_clerk",
        "restart_in_flight_work",
        "evidence_controls_read_only",
        "recovery_ceremony",
    )

    duplicated = _report_payload()
    duplicated["scenarios"][1]["scenario_id"] = duplicated["scenarios"][0]["scenario_id"]
    _seal(duplicated)
    with pytest.raises(ValidationError, match="scenario IDs must be unique"):
        SyntheticCustodyRehearsalReport.model_validate(duplicated)

    incomplete = _report_payload()
    incomplete["scenarios"].pop()
    _seal(incomplete)
    with pytest.raises(ValidationError):
        SyntheticCustodyRehearsalReport.model_validate(incomplete)


def test_schema_compatibility_exports_share_the_typed_scenario_registry() -> None:
    assert SyntheticAbortCause is DomainSyntheticAbortCause
    assert SyntheticAbortCause is RegistrySyntheticAbortCause
    assert SyntheticScenarioCategory is DomainSyntheticScenarioCategory
    assert tuple(scenario.scenario_id for scenario in SYNTHETIC_REHEARSAL_SCENARIOS) == (REGISTRY_SCENARIO_IDS)
    assert all(
        isinstance(scenario.category, SyntheticScenarioCategory)
        and isinstance(scenario.abort_cause, SyntheticAbortCause)
        and scenario.test_refs
        for scenario in SYNTHETIC_REHEARSAL_SCENARIOS
    )
    assert set(REGISTRY_SCENARIO_IDS) == set(SyntheticScenarioId)


def test_schema_depends_on_neutral_synthetic_domain_not_execution_registry() -> None:
    schema_source = (Path(__file__).parents[2] / "app/schemas/account_custody_qualification.py").read_text()

    assert "app.services.account_custody_synthetic_scenarios" not in schema_source


def test_synthetic_report_validates_content_hash_over_semantic_payload() -> None:
    payload = _report_payload()
    report = SyntheticCustodyRehearsalReport.model_validate(payload)

    assert report.report_sha256 == account_custody_qualification_payload_sha256(
        report.model_dump(mode="json", exclude={"report_sha256"})
    )

    payload["generated_at_ms"] += 1
    with pytest.raises(ValidationError, match="SHA-256"):
        SyntheticCustodyRehearsalReport.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("production_mount_access", "READ_WRITE"),
        ("qualification_mount_access", "READ_ONLY"),
        ("production_generation_opened_read_write", True),
        ("mount_identity_separated", False),
        ("qualification_mount_id", 12),
    ),
)
def test_protected_generation_rejects_unproven_production_isolation(
    field: str,
    value: object,
) -> None:
    payload = _report_payload()
    payload["protected_generation"][field] = value
    _seal(payload)

    with pytest.raises(ValidationError):
        SyntheticCustodyRehearsalReport.model_validate(payload)


@pytest.mark.parametrize(
    ("field_name", "forbidden_value"),
    (
        ("live_environment_status", "LIVE_PASSED"),
        ("overall_status", "ACCEPTED"),
        ("label", "LIVE_ACCEPTED"),
        ("adr_0035_status", "ACCEPTED"),
    ),
)
def test_synthetic_report_cannot_claim_live_or_accepted(
    field_name: str,
    forbidden_value: str,
) -> None:
    payload = _report_payload()
    payload[field_name] = forbidden_value
    _seal(payload)

    with pytest.raises(ValidationError):
        SyntheticCustodyRehearsalReport.model_validate(payload)


@pytest.mark.parametrize(
    ("abort_class", "failure_detail"),
    (
        ("FEED_ABORT", None),
        ("NONE", "unexpected failure text"),
    ),
)
def test_passing_scenario_requires_no_abort_and_no_failure_detail(
    abort_class: str,
    failure_detail: str | None,
) -> None:
    payload = _scenario_payload("feed_one_print_stall", 0)
    payload["abort_class"] = abort_class
    payload["failure_detail"] = failure_detail

    with pytest.raises(ValidationError, match="rehearsed row"):
        SyntheticScenarioResult.model_validate(payload)


@pytest.mark.parametrize(
    ("abort_class", "failure_detail"),
    (
        ("NONE", "feed stopped"),
        ("FEED_ABORT", None),
    ),
)
def test_aborted_scenario_requires_classification_and_failure_detail(
    abort_class: str,
    failure_detail: str | None,
) -> None:
    payload = _scenario_payload("feed_one_print_stall", 0)
    payload["status"] = "ABORTED"
    payload["abort_class"] = abort_class
    payload["failure_detail"] = failure_detail

    with pytest.raises(ValidationError, match="aborted row"):
        SyntheticScenarioResult.model_validate(payload)


def test_required_seam_execution_failure_is_an_infrastructure_abort() -> None:
    payload = _scenario_payload("lost_submit_exact_identity", 0)
    payload.update(
        status="ABORTED",
        abort_class="INFRA_ABORT",
        abort_cause="INFRASTRUCTURE_OR_TOOLING_FAILURE",
        failure_detail="the seam subprocess timed out before reaching the injection",
        fault_seam_required=True,
    )

    scenario = SyntheticScenarioResult.model_validate(payload)

    assert scenario.fault_seam_exercised is False
    assert scenario.abort_class == "INFRA_ABORT"
    assert scenario.limitations == ()


def test_limited_scenario_requires_typed_limitations_without_abort() -> None:
    payload = _scenario_payload("lost_submit_exact_identity", 0)
    payload["status"] = "REHEARSED_WITH_LIMITATIONS_PENDING_LIVE_CONFIRMATION"
    payload["limitations"] = [
        {
            "code": "WRITE_TIMEOUT_PRE_SDK",
            "fault_kind": "timeout",
            "detail": "The existing seam cannot model a landed order with a lost response.",
        }
    ]

    scenario = SyntheticScenarioResult.model_validate(payload)

    assert scenario.limitations[0].code == "WRITE_TIMEOUT_PRE_SDK"
    payload["limitations"] = []
    with pytest.raises(ValidationError, match="limited rehearsal"):
        SyntheticScenarioResult.model_validate(payload)


@pytest.mark.parametrize(
    ("scenario_id", "abort_class"),
    (
        ("feed_one_print_stall", "FEED_ABORT"),
        ("lost_submit_exact_identity", "CUSTODY_ABORT"),
        ("evidence_controls_read_only", "CUSTODY_ABORT"),
        ("recovery_ceremony", "CUSTODY_ABORT"),
    ),
)
def test_synthetic_report_preserves_feed_and_custody_abort_classifications(
    scenario_id: str,
    abort_class: str,
) -> None:
    payload = _report_payload()
    _abort_scenario(payload, scenario_id=scenario_id, abort_class=abort_class)

    report = SyntheticCustodyRehearsalReport.model_validate(payload)

    assert report.overall_status == "ABORTED"
    assert report.abort_class == abort_class


def test_synthetic_report_gives_custody_abort_precedence_over_feed_abort() -> None:
    payload = _report_payload()
    _abort_scenario(
        payload,
        scenario_id="feed_one_print_stall",
        abort_class="FEED_ABORT",
    )
    custody = next(row for row in payload["scenarios"] if row["scenario_id"] == "lost_submit_exact_identity")
    custody["status"] = "ABORTED"
    custody["abort_class"] = "CUSTODY_ABORT"
    custody["abort_cause"] = "BROKER_MUTATION_WITHOUT_FINALIZED_INTENT"
    custody["failure_detail"] = "lost submit deliberately failed"
    payload["abort_class"] = "CUSTODY_ABORT"
    _seal(payload)

    report = SyntheticCustodyRehearsalReport.model_validate(payload)

    assert report.abort_class == "CUSTODY_ABORT"


@pytest.mark.parametrize(
    "cause",
    (
        SyntheticAbortCause.DUPLICATE_ECONOMIC_INTENT,
        SyntheticAbortCause.BROKER_MUTATION_WITHOUT_FINALIZED_INTENT,
        SyntheticAbortCause.MIXED_AUTHORITY_WRITERS,
        SyntheticAbortCause.UNEXPLAINED_CUSTODY_DRIFT,
        SyntheticAbortCause.CUSTODY_STATE_REGRESSION,
        SyntheticAbortCause.FABRICATED_TERMINAL_RESULT,
        SyntheticAbortCause.STALE_OR_UNKNOWN_EVIDENCE_AUTHORIZED_EXPOSURE,
        SyntheticAbortCause.CORRUPT_MISSING_OR_SUBSTITUTED_AUTHORITY,
        SyntheticAbortCause.RECOVERY_HASH_IDENTITY_FAILURE,
        SyntheticAbortCause.UI_EXECUTION_POLICY_MISMATCH,
    ),
)
def test_named_custody_abort_causes_share_one_classifier(
    cause: SyntheticAbortCause,
) -> None:
    assert synthetic_abort_class_for_cause(cause) == "CUSTODY_ABORT"


def test_ui_execution_policy_failure_cannot_be_misclassified_as_infrastructure() -> None:
    payload = _scenario_payload("evidence_controls_read_only", 0)
    payload.update(
        status="ABORTED",
        abort_class="INFRA_ABORT",
        abort_cause="UI_EXECUTION_POLICY_MISMATCH",
        failure_detail="the browser harness failed",
    )

    with pytest.raises(ValidationError, match="custody-finding"):
        SyntheticScenarioResult.model_validate(payload)


@pytest.mark.parametrize(
    ("source_event_at_ms", "clerk_observed_at_ms", "recorded_at_ms"),
    (
        (1_002, 1_001, 1_003),
        (1_000, 1_003, 1_002),
    ),
)
def test_worksheet_rejects_out_of_order_clocks(
    source_event_at_ms: int,
    clerk_observed_at_ms: int,
    recorded_at_ms: int,
) -> None:
    payload = _worksheet_payload()
    payload.update(
        source_event_at_ms=source_event_at_ms,
        clerk_observed_at_ms=clerk_observed_at_ms,
        recorded_at_ms=recorded_at_ms,
    )

    with pytest.raises(ValidationError, match="worksheet clocks"):
        SyntheticEvidenceWorksheet.model_validate(payload)


def test_worksheet_preserves_explicit_authority_and_request_identity_fields() -> None:
    payload = _worksheet_payload()

    worksheet = SyntheticEvidenceWorksheet.model_validate(payload)
    dumped = worksheet.model_dump(mode="json")

    assert dumped["authority_generation"] == 2
    assert dumped["db_identity_token"] == "qualification-db-token"
    assert dumped["control_revision"] == 17
    assert dumped["command_ids"] == ["command-1415"]
    assert dumped["effect_operation_ids"] == ["effect-1415"]
    assert dumped["order_refs"] == ["order-ref-1415"]
    assert dumped["broker_order_ids"] == ["broker-order-1415"]
    assert dumped["receipt_ids"] == ["receipt-1415"]
    assert dumped["source_event_at_ms"] <= dumped["clerk_observed_at_ms"] <= dumped["recorded_at_ms"]


def test_report_rejects_wrong_overall_abort_class_even_with_valid_hash() -> None:
    payload = _report_payload()
    _abort_scenario(
        payload,
        scenario_id="feed_never_first_bar",
        abort_class="FEED_ABORT",
    )
    payload["abort_class"] = "CUSTODY_ABORT"
    _seal(payload)

    with pytest.raises(ValidationError, match="overall abort class"):
        SyntheticCustodyRehearsalReport.model_validate(deepcopy(payload))


def test_core_phase_abort_receipt_is_content_addressed_and_cannot_claim_live() -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_ms": 1_800_000_000_000,
        "label": "PRE_LIVE_REHEARSAL_SYNTHETIC_ABORT",
        "live_environment_status": "NOT_RUN",
        "adr_0035_status": "PROPOSED",
        "overall_status": "ABORTED",
        "abort_class": "INFRA_ABORT",
        "abort_cause": "INFRASTRUCTURE_OR_TOOLING_FAILURE",
        "phase": "PROTECTED_STORAGE_OR_RECOVERY_TOOLING",
        "failure_type": "RuntimeError",
        "failure_detail": "XFS boundary was unavailable",
    }
    payload["report_sha256"] = account_custody_qualification_payload_sha256(payload)

    report = SyntheticHarnessAbortReport.model_validate(payload)

    assert report.abort_class == "INFRA_ABORT"
    payload["live_environment_status"] = "LIVE_PASSED"
    payload["report_sha256"] = account_custody_qualification_payload_sha256(
        {key: value for key, value in payload.items() if key != "report_sha256"}
    )
    with pytest.raises(ValidationError):
        SyntheticHarnessAbortReport.model_validate(payload)


def test_core_harness_phase_cannot_claim_a_custody_finding() -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_ms": 1_800_000_000_000,
        "label": "PRE_LIVE_REHEARSAL_SYNTHETIC_ABORT",
        "live_environment_status": "NOT_RUN",
        "adr_0035_status": "PROPOSED",
        "overall_status": "ABORTED",
        "abort_class": "CUSTODY_ABORT",
        "abort_cause": "INFRASTRUCTURE_OR_TOOLING_FAILURE",
        "phase": "INTEGRATED_CUSTODY_DRILLS",
        "failure_type": "TimeoutError",
        "failure_detail": "the harness timed out before producing a finding",
    }
    payload["report_sha256"] = account_custody_qualification_payload_sha256(payload)

    with pytest.raises(ValidationError):
        SyntheticHarnessAbortReport.model_validate(payload)


def test_recovery_integrity_abort_receipt_requires_custody_classification() -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_ms": 1_800_000_000_000,
        "label": "PRE_LIVE_REHEARSAL_SYNTHETIC_ABORT",
        "live_environment_status": "NOT_RUN",
        "adr_0035_status": "PROPOSED",
        "overall_status": "ABORTED",
        "abort_class": "CUSTODY_ABORT",
        "abort_cause": "RECOVERY_HASH_IDENTITY_FAILURE",
        "phase": "RECOVERY_INTEGRITY",
        "failure_type": "SyntheticRecoveryCustodyError",
        "failure_detail": "restore and rebuild hash heads disagree",
    }
    payload["report_sha256"] = account_custody_qualification_payload_sha256(payload)

    report = SyntheticHarnessAbortReport.model_validate(payload)

    assert report.abort_class == "CUSTODY_ABORT"
    payload["abort_class"] = "INFRA_ABORT"
    payload["report_sha256"] = account_custody_qualification_payload_sha256(
        {key: value for key, value in payload.items() if key != "report_sha256"}
    )
    with pytest.raises(ValidationError, match="core phase abort class"):
        SyntheticHarnessAbortReport.model_validate(payload)
