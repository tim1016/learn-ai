"""Acceptance and regression tests for the broker-free custody S15 runner."""

from __future__ import annotations

import json

import pytest

from app.schemas.account_custody_qualification import (
    BACKEND_CUSTODY_QUALIFICATION_DRILL_IDS,
    INT64_MAX,
    AccountCustodyQualificationReport,
)
from app.services.account_custody_qualification import (
    DeterministicFaultControls,
    nearest_rank_percentile,
    run_deterministic_account_custody_qualification,
    verify_account_custody_qualification_report,
)
from scripts.run_account_custody_qualification import main

EXPECTED_DRILL_NAMES = (
    "queue_delay_before_a0",
    "fsync_delay",
    "qualification_delay_after_a0",
    "socket_loss_after_a0_before_a1",
    "socket_loss_after_a1_before_a2",
    "fill_before_ack_callback",
    "duplicate_and_reordered_callbacks",
    "originator_death_after_a0",
    "retired_originator_late_fill",
    "clerk_death_with_nonterminal_intents",
    "ibkr_1101_and_1102",
    "callback_silence_with_socket_connected",
    "pause_stop_during_daemon_outage",
    "start_resume_deploy_during_outage",
    "flatten_with_stale_positions",
    "operational_log_concurrency",
    "outage_diff",
)


def test_default_campaign_covers_every_prd_drill_with_complete_receipts() -> None:
    report = run_deterministic_account_custody_qualification(
        account_id="DU1234567",
        generated_at_ms=1_784_950_000_000,
    )

    assert report.execution_mode == "DETERMINISTIC"
    assert report.broker_dependency == "NONE"
    assert report.fleet_size == 8
    assert report.entry_capacity == 8
    assert report.risk_reducing_capacity == 0
    assert tuple(drill.name for drill in report.drills) == EXPECTED_DRILL_NAMES
    assert tuple(drill.drill_id for drill in report.drills) == BACKEND_CUSTODY_QUALIFICATION_DRILL_IDS
    assert all(drill.passed for drill in report.drills)
    assert all(drill.observed_receipts and drill.evidence_refs for drill in report.drills)
    assert all(drill.failure_detail is None for drill in report.drills)
    assert report.metrics.queue_high_water == 8
    assert report.metrics.queue_refusal_count == 1
    assert report.metrics.projection_gap_count == 0
    retired_fill = report.drills[8]
    assert any(":broker_event:" in receipt for receipt in retired_fill.observed_receipts)
    assert "verdict:SUSPENDED" in retired_fill.observed_receipts
    stale_action = next(drill for drill in report.drills if drill.drill_id == 16)
    assert stale_action.observed_receipts[0].startswith("flatten_authorization:CLERK_EMERGENCY_RECONCILIATION_")
    assert "broker_calls:0" in stale_action.observed_receipts
    assert report.certificate.status == "DETERMINISTIC_PASSED_AWAITING_PAPER"
    assert report.certificate.paper_environment_status == "NOT_RUN"


def test_campaign_fault_controls_map_to_non_overlapping_receipt_intervals() -> None:
    controls = DeterministicFaultControls(
        queue_delay_ms=41,
        fsync_delay_ms=43,
        qualification_delay_ms=47,
        broker_write_delay_ms=53,
        callback_delay_ms=59,
        broker_ack_delay_ms=61,
    )
    report = run_deterministic_account_custody_qualification(
        account_id="DU1234567",
        generated_at_ms=1_784_950_000_000,
        controls=controls,
    )
    p50_by_interval = {metric.name: metric.p50_ms for metric in report.metrics.phase_latencies}

    assert p50_by_interval == {
        "receipt_request_to_intake_admission": 41,
        "receipt_intake_admission_to_inbox_fsync": 43,
        "receipt_a0_recorded_to_a1_submitting": 47,
        "receipt_a1_submitting_to_callback": 112,
        "receipt_callback_to_ack": 61,
        "receipt_epoch_notice_to_recovery": 240,
    }


def test_report_hash_is_reproducible_and_rejects_semantic_tampering() -> None:
    first = run_deterministic_account_custody_qualification(
        account_id="DU1234567",
        generated_at_ms=1_784_950_000_000,
    )
    second = run_deterministic_account_custody_qualification(
        account_id="DU1234567",
        generated_at_ms=1_784_950_000_000,
    )

    assert first.report_sha256 == second.report_sha256
    assert verify_account_custody_qualification_report(first)
    assert not verify_account_custody_qualification_report(first.model_copy(update={"account_id": "DU7654321"}))


def test_failed_deterministic_campaign_stays_unqualified_for_paper_execution() -> None:
    controls = DeterministicFaultControls(queue_delay_ms=10_001)

    report = run_deterministic_account_custody_qualification(
        account_id="DU1234567",
        generated_at_ms=1_784_950_000_000,
        controls=controls,
    )

    assert report.certificate.status == "FAILED"
    assert report.certificate.failed_drill_ids
    assert report.certificate.paper_environment_status == "NOT_RUN"
    assert all(drill.failure_detail is not None for drill in report.drills if not drill.passed)


def test_nearest_rank_percentiles_are_pinned() -> None:
    values = tuple(range(1, 101))

    assert nearest_rank_percentile(values, 50) == 50
    assert nearest_rank_percentile(values, 95) == 95
    assert nearest_rank_percentile(values, 99) == 99


@pytest.mark.parametrize("values, percentile", [((), 50), ((-1,), 50), ((1,), 0), ((1,), 101)])
def test_nearest_rank_percentile_rejects_invalid_inputs(values: tuple[int, ...], percentile: int) -> None:
    with pytest.raises(ValueError):
        nearest_rank_percentile(values, percentile)


def test_report_rejects_tampered_json_and_out_of_int64_timestamps() -> None:
    report = run_deterministic_account_custody_qualification(
        account_id="DU1234567",
        generated_at_ms=1_784_950_000_000,
    )
    payload = report.model_dump(mode="json")
    payload["metrics"]["queue_refusal_count"] = 9

    with pytest.raises(ValueError, match="SHA-256"):
        AccountCustodyQualificationReport.model_validate(payload)
    with pytest.raises(ValueError, match="int64"):
        run_deterministic_account_custody_qualification(
            account_id="DU1234567",
            generated_at_ms=INT64_MAX + 1,
        )


def test_cli_archives_verifiable_backend_report(tmp_path, capsys) -> None:
    output = tmp_path / "qualification.json"

    exit_code = main(
        [
            "--account-id",
            "DU1234567",
            "--output",
            str(output),
            "--generated-at-ms",
            "1784950000000",
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["certificate"]["status"] == "DETERMINISTIC_PASSED_AWAITING_PAPER"
    assert len(payload["drills"]) == 17
    assert json.loads(capsys.readouterr().out)["report_sha256"] == payload["report_sha256"]


def test_cli_rejects_self_authored_paper_promotion_arguments(tmp_path) -> None:
    with pytest.raises(SystemExit) as rejected:
        main(
            [
                "--account-id",
                "DU1234567",
                "--output",
                str(tmp_path / "qualification.json"),
                "--paper-receipt",
                str(tmp_path / "self-authored.json"),
            ]
        )

    assert rejected.value.code == 2
