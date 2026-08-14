"""Regression coverage for the manual-order pre-live evidence command."""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.services.manual_order_qualification import MANUAL_QUALIFICATION_SCENARIOS
from scripts import run_manual_order_qualification as runner


def test_manual_qualification_matrix_covers_each_required_pre_live_shape() -> None:
    scenario_ids = {scenario.scenario_id for scenario in MANUAL_QUALIFICATION_SCENARIOS}

    assert scenario_ids == {
        "offline_v8_to_v9_ceremony",
        "market_buy_fill_and_projection",
        "manual_owned_reduction",
        "limit_and_cancel",
        "duplicate_submit_and_reload",
        "restart_and_exact_reconciliation",
        "coverage_conflict_recovery",
        "ordered_ticket_pause",
    }
    assert all(scenario.test_refs for scenario in MANUAL_QUALIFICATION_SCENARIOS)


def test_pre_live_report_does_not_claim_live_paper_qualification() -> None:
    scenarios = [
        {
            "scenario_id": scenario.scenario_id,
            "title": scenario.title,
            "test_refs": list(scenario.test_refs),
            "status": "PASSED",
            "started_at_ms": 1,
            "completed_at_ms": 2,
            "stdout_sha256": "a" * 64,
            "stderr_sha256": "b" * 64,
            "failure_detail": None,
        }
        for scenario in MANUAL_QUALIFICATION_SCENARIOS
    ]

    source_identity = {
        "status": "VERIFIED",
        "git_commit": "a" * 40,
        "working_tree_clean": True,
        "source_sha256": {"PythonDataService/scripts/run_manual_order_qualification.py": "c" * 64},
        "missing_sources": [],
    }
    report = runner._report(
        scenarios=scenarios,
        generated_at_ms=3,
        source_identity=source_identity,
    )

    assert report["overall_status"] == "PRE_LIVE_REHEARSAL_PASSED"
    assert report["live_environment_status"] == "NOT_RUN"
    assert report["release_gate_status"] == "PENDING_DATED_PAPER_CEREMONY"
    assert report["tested_revision"] == source_identity
    assert report["report_sha256"] == runner._sha256(
        runner._canonical_json({key: value for key, value in report.items() if key != "report_sha256"})
    )


def test_unverifiable_source_identity_refuses_a_passing_pre_live_receipt() -> None:
    report = runner._report(
        scenarios=[
            {
                "scenario_id": "scenario",
                "title": "Scenario",
                "test_refs": ["tests/example.py::test_example"],
                "status": "PASSED",
                "started_at_ms": 1,
                "completed_at_ms": 2,
                "stdout_sha256": "a" * 64,
                "stderr_sha256": "b" * 64,
                "failure_detail": None,
            }
        ],
        generated_at_ms=3,
        source_identity={
            "status": "UNVERIFIABLE",
            "git_commit": None,
            "working_tree_clean": False,
            "source_sha256": {},
            "missing_sources": ["tests/example.py"],
        },
    )

    assert report["overall_status"] == "PRE_LIVE_REHEARSAL_FAILED"


def test_scenario_failure_captures_bounded_diagnostic_without_claiming_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scenario = MANUAL_QUALIFICATION_SCENARIOS[0]

    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["pytest"],
            returncode=1,
            stdout="failed assertion",
            stderr="broker-free regression failed",
        ),
    )

    result = runner._run_scenario(
        scenario,
        working_directory=tmp_path,
        now_ms=iter((1, 2)).__next__,
    )

    assert result["status"] == "FAILED"
    assert result["failure_detail"] == (
        "pytest returned 1; stdout tail: failed assertion; stderr tail: broker-free regression failed"
    )
    assert result["stdout_sha256"] == runner._sha256("failed assertion")
