"""Tests for the bounded synthetic rehearsal command surface."""

from __future__ import annotations

import hashlib
import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from app.broker.alpaca.clerk.sqlite.qualification import SyntheticRehearsalPhaseError
from app.broker.alpaca.clerk.sqlite.qualification_storage_recovery import (
    PRODUCTION_ARTIFACTS_ROOT,
    QUALIFICATION_ARTIFACTS_ROOT,
)
from app.broker.alpaca.clerk.sqlite.qualification_ui_campaign_contract import (
    BOUNDED_UI_CAMPAIGN,
    UI_CAMPAIGN_CONTRACT_SHA256,
)
from app.services.account_custody_synthetic_scenarios import (
    SYNTHETIC_REHEARSAL_SCENARIOS,
    SyntheticAbortCause,
    SyntheticScenarioCategory,
    SyntheticScenarioId,
    synthetic_scenario,
)
from scripts import run_alpaca_sqlite_qualification as runner
from scripts.run_alpaca_sqlite_qualification import (
    _MAX_FAILURE_DETAIL_CHARS,
    _bounded_process_failure,
    _load_preverified_ui_outcome,
    _parse_args,
    _run_synthetic_command,
    _run_synthetic_rehearsal,
    _run_synthetic_scenario,
    _ui_test_command,
)
from tests._helpers.ui_correlation import ui_correlation_records

_UI_TEST_REF = synthetic_scenario(SyntheticScenarioId.EVIDENCE_CONTROLS_READ_ONLY).test_refs[0]
_UI_SOURCE_BYTES = b"test('bounded qualification campaign', () => undefined);\n"
_UI_SOURCE_SHA256 = hashlib.sha256(_UI_SOURCE_BYTES).hexdigest()


def _write_ui_source(tmp_path: Path) -> Path:
    source = tmp_path / "alpaca-clerk-ui-correlation.spec.ts"
    source.write_bytes(_UI_SOURCE_BYTES)
    return source


def _valid_ui_receipt() -> dict[str, object]:
    correlation_records = list(ui_correlation_records())
    return {
        "schema_version": 5,
        "test_ref": _UI_TEST_REF,
        "command": list(_ui_test_command()),
        "git_commit_sha": "b66ffaebea5dc33c07d82a81ad8f818113b2f089",
        "test_source_sha256": _UI_SOURCE_SHA256,
        "campaign_contract_sha256": UI_CAMPAIGN_CONTRACT_SHA256,
        "executed_at_ms": 1_807_123_456_789,
        "duration_ms": 12_345,
        "return_code": 0,
        "stdout_sha256": "1" * 64,
        "stderr_sha256": "2" * 64,
        "measurements": {
            "campaign_id": "frontend-browser-network-correlation-1413",
            "page_load_count": 5,
            "browser_reload_count": 4,
            "interaction_count": 25,
            "evidence_request_count": 25,
            "evidence_response_proof_reference_count": 25,
            "native_event_source_connection_count": 25,
            "lifecycle_request_count": 0,
            "observed_revision_count": 25,
            "lifecycle_request_action_ids": [],
            "revision_sequence": list(BOUNDED_UI_CAMPAIGN.revision_sequence),
            "historical_snapshot_state": "OFF_DUTY_STOPPED_FLAT_POST_RESTART",
            "presented_action_id": "resume",
            "correlation_record_count": 25,
        },
        "correlation_records": correlation_records,
        "evidence_refs": ["ci-run:1415:operator-lens"],
    }


def test_profile_arguments_remain_backward_compatible(tmp_path: Path) -> None:
    args = _parse_args(
        [
            "--profile",
            "smoke",
            "--json-output",
            str(tmp_path / "qualification.json"),
            "--markdown-output",
            str(tmp_path / "qualification.md"),
            "--skip-adversarial",
        ]
    )

    assert args.profile == "smoke"
    assert args.synthetic_rehearsal is False
    assert args.skip_adversarial is True


def test_synthetic_arguments_are_complete_and_mutually_exclusive(tmp_path: Path) -> None:
    args = _parse_args(
        [
            "--synthetic-rehearsal",
            "--artifacts-root",
            str(tmp_path / "artifacts"),
            "--production-artifacts-root",
            str(tmp_path / "production-artifacts"),
            "--fixture-dir",
            str(tmp_path / "fixture"),
            "--production-account-id",
            "PAPER-PRODUCTION",
            "--json-output",
            str(tmp_path / "rehearsal.json"),
            "--markdown-output",
            str(tmp_path / "rehearsal.md"),
        ]
    )

    assert args.synthetic_rehearsal is True
    assert args.profile is None
    assert args.production_account_id == "PAPER-PRODUCTION"
    assert args.production_artifacts_root == tmp_path / "production-artifacts"

    with pytest.raises(SystemExit):
        _parse_args(
            [
                "--synthetic-rehearsal",
                "--json-output",
                str(tmp_path / "incomplete.json"),
                "--markdown-output",
                str(tmp_path / "incomplete.md"),
            ]
        )


def test_preverified_ui_receipt_is_exact_bounded_and_content_addressed(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "ui-evidence.json"
    encoded = json.dumps(_valid_ui_receipt(), sort_keys=True).encode()
    receipt.write_bytes(encoded)
    source = _write_ui_source(tmp_path)

    outcome = _load_preverified_ui_outcome(
        receipt,
        ui_source_path=source,
        expected_git_commit_sha=str(_valid_ui_receipt()["git_commit_sha"]),
    )

    assert outcome.status == "PASSED"
    assert outcome.evidence_refs == (
        _UI_TEST_REF,
        "ci-run:1415:operator-lens",
        "git-commit:b66ffaebea5dc33c07d82a81ad8f818113b2f089",
        f"test-source-sha256:{_UI_SOURCE_SHA256}",
        f"campaign-contract-sha256:{UI_CAMPAIGN_CONTRACT_SHA256}",
        f"stdout-sha256:{'1' * 64}",
        f"stderr-sha256:{'2' * 64}",
        f"preverified-ui-sha256:{hashlib.sha256(encoded).hexdigest()}",
    )


def test_preverified_ui_receipt_requires_the_exact_test_source(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "ui-evidence.json"
    receipt.write_text(json.dumps(_valid_ui_receipt()), encoding="utf-8")

    with pytest.raises(TypeError, match="ui_source_path"):
        _load_preverified_ui_outcome(receipt)


def test_preverified_ui_receipt_authenticates_the_exact_test_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "alpaca-clerk-ui-correlation.spec.ts"
    source.write_text("test('bounded campaign', () => undefined);\n", encoding="utf-8")
    payload = _valid_ui_receipt()
    payload["test_source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    receipt = tmp_path / "ui-evidence.json"
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    outcome = _load_preverified_ui_outcome(
        receipt,
        ui_source_path=source,
        expected_git_commit_sha=str(payload["git_commit_sha"]),
    )

    assert outcome.status == "PASSED"
    source.write_text("test('changed campaign', () => undefined);\n", encoding="utf-8")
    with pytest.raises(ValueError, match="authenticate the mounted test source"):
        _load_preverified_ui_outcome(receipt, ui_source_path=source)


def test_preverified_ui_receipt_rejects_a_different_image_commit(
    tmp_path: Path,
) -> None:
    source = _write_ui_source(tmp_path)
    receipt = tmp_path / "ui-evidence.json"
    receipt.write_text(json.dumps(_valid_ui_receipt()), encoding="utf-8")

    with pytest.raises(ValueError, match="commit does not match"):
        _load_preverified_ui_outcome(
            receipt,
            ui_source_path=source,
            expected_git_commit_sha="f" * 40,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 4),
        ("test_ref", "wrong"),
        ("command", ["npx", "ng", "test"]),
        ("git_commit_sha", "short"),
        ("test_source_sha256", "short"),
        ("campaign_contract_sha256", "0" * 64),
        ("executed_at_ms", "2026-08-07T00:00:00Z"),
        ("duration_ms", 600_001),
        ("return_code", 1),
        ("stdout_sha256", "not-a-digest"),
        ("stderr_sha256", "A" * 64),
        ("evidence_refs", []),
    ],
)
def test_preverified_ui_receipt_rejects_nonpassing_or_unbounded_shapes(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = _valid_ui_receipt()
    payload[field] = value
    receipt = tmp_path / "ui-evidence.json"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    source = _write_ui_source(tmp_path)

    with pytest.raises(ValueError):
        _load_preverified_ui_outcome(receipt, ui_source_path=source)


def test_preverified_ui_receipt_rejects_unmeasured_or_mismatched_campaign(
    tmp_path: Path,
) -> None:
    payload = _valid_ui_receipt()
    payload["measurements"] = {
        "campaign_id": "frontend-browser-network-correlation-1413",
        "page_load_count": 5,
        "browser_reload_count": 4,
        "interaction_count": 25,
        "evidence_request_count": 25,
        "evidence_response_proof_reference_count": 25,
        "native_event_source_connection_count": 25,
        "lifecycle_request_count": 1,
        "observed_revision_count": 25,
        "lifecycle_request_action_ids": ["resume_bot"],
        "revision_sequence": list(BOUNDED_UI_CAMPAIGN.revision_sequence),
        "historical_snapshot_state": "OFF_DUTY_STOPPED_FLAT_POST_RESTART",
        "presented_action_id": "resume",
        "correlation_record_count": 25,
    }
    receipt = tmp_path / "ui-evidence.json"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    source = _write_ui_source(tmp_path)

    with pytest.raises(ValueError, match="bounded campaign"):
        _load_preverified_ui_outcome(receipt, ui_source_path=source)


@pytest.mark.parametrize(
    "mutation",
    ("drop_record", "dispatch_resume"),
)
def test_preverified_ui_receipt_rejects_incomplete_or_actionful_correlation(
    tmp_path: Path,
    mutation: str,
) -> None:
    payload = _valid_ui_receipt()
    records = payload["correlation_records"]
    assert isinstance(records, list)
    if mutation == "drop_record":
        records.pop()
    else:
        records[0]["dispatched_lifecycle_action_id"] = "resume"
    receipt = tmp_path / "ui-evidence.json"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    source = _write_ui_source(tmp_path)

    with pytest.raises(ValueError, match="correlation"):
        _load_preverified_ui_outcome(receipt, ui_source_path=source)


def test_preverified_ui_receipt_rejects_status_only_claim(tmp_path: Path) -> None:
    receipt = tmp_path / "ui-evidence.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "test_ref": _UI_TEST_REF,
                "status": "PASSED",
                "evidence_refs": ["unmeasured-claim"],
            }
        ),
        encoding="utf-8",
    )
    source = _write_ui_source(tmp_path)

    with pytest.raises(ValueError, match="unexpected shape"):
        _load_preverified_ui_outcome(receipt, ui_source_path=source)


def test_recovery_scenario_is_pending_until_core_validates_the_ceremony(
    tmp_path: Path,
) -> None:
    recovery = next(
        scenario
        for scenario in SYNTHETIC_REHEARSAL_SCENARIOS
        if scenario.category is SyntheticScenarioCategory.RECOVERY
    )

    outcome = _run_synthetic_scenario(
        recovery,
        repository_root=tmp_path,
        service_root=tmp_path,
        preverified_ui_evidence=None,
    )

    assert outcome.status == "PENDING_CORE_VALIDATION"
    assert outcome.log_refs == ("runner:pending-protected-generation-recovery-ceremony",)


def test_ui_scenario_requires_authenticated_measured_evidence(tmp_path: Path) -> None:
    ui_scenario = synthetic_scenario(SyntheticScenarioId.EVIDENCE_CONTROLS_READ_ONLY)

    outcome = _run_synthetic_scenario(
        ui_scenario,
        repository_root=tmp_path,
        service_root=tmp_path,
        preverified_ui_evidence=None,
    )

    assert outcome.status == "FAILED"
    assert outcome.failure_origin == "INFRASTRUCTURE_OR_TOOLING_FAILURE"
    assert outcome.failure_detail is not None
    assert "preverified-ui-evidence is required" in outcome.failure_detail


def test_synthetic_subprocess_timeout_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired(
            cmd=["pytest"],
            timeout=7,
            output="bounded stdout",
            stderr="bounded stderr",
        )

    monkeypatch.setattr(subprocess, "run", timeout)

    outcome = _run_synthetic_command(
        ["pytest"],
        harness=runner._SyntheticHarness.PYTEST,
        cwd=tmp_path,
        timeout_seconds=7,
        evidence_refs=("test-ref",),
        log_prefix="pytest:timeout-test",
    )

    assert outcome.status == "FAILED"
    assert outcome.failure_origin == "INFRASTRUCTURE_OR_TOOLING_FAILURE"
    assert outcome.log_refs == ("pytest:timeout-test:timeout:7s",)
    assert outcome.failure_detail is not None
    assert "bounded 7s timeout" in outcome.failure_detail


def test_synthetic_subprocess_os_error_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise FileNotFoundError("qualification executable missing")

    monkeypatch.setattr(subprocess, "run", missing)

    outcome = _run_synthetic_command(
        ["missing"],
        harness=runner._SyntheticHarness.PYTEST,
        cwd=tmp_path,
        timeout_seconds=7,
        evidence_refs=("test-ref",),
        log_prefix="pytest:os-error-test",
    )

    assert outcome.status == "FAILED"
    assert outcome.failure_origin == "INFRASTRUCTURE_OR_TOOLING_FAILURE"
    assert outcome.log_refs == ("pytest:os-error-test:os-error:FileNotFoundError",)
    assert outcome.failure_detail is not None
    assert "qualification executable missing" in outcome.failure_detail


@pytest.mark.parametrize(
    "exception",
    (
        subprocess.TimeoutExpired(cmd=["pytest"], timeout=7),
        FileNotFoundError("qualification executable missing"),
    ),
)
def test_custody_scenario_runner_marks_execution_failures_as_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception: BaseException,
) -> None:
    custody_scenario = next(
        scenario
        for scenario in SYNTHETIC_REHEARSAL_SCENARIOS
        if scenario.scenario_id is SyntheticScenarioId.LOST_SUBMIT_EXACT_IDENTITY
    )

    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise exception

    monkeypatch.setattr(subprocess, "run", fail)

    outcome = _run_synthetic_scenario(
        custody_scenario,
        repository_root=tmp_path,
        service_root=tmp_path,
        preverified_ui_evidence=None,
    )

    assert outcome.status == "FAILED"
    assert outcome.failure_origin == "INFRASTRUCTURE_OR_TOOLING_FAILURE"


@pytest.mark.parametrize(
    ("return_code", "report_element", "failure_origin"),
    (
        (1, "failure", "OBSERVED_INVARIANT_FAILURE"),
        (1, "error", "INFRASTRUCTURE_OR_TOOLING_FAILURE"),
        (2, None, "INFRASTRUCTURE_OR_TOOLING_FAILURE"),
        (3, None, "INFRASTRUCTURE_OR_TOOLING_FAILURE"),
        (4, None, "INFRASTRUCTURE_OR_TOOLING_FAILURE"),
        (5, None, "INFRASTRUCTURE_OR_TOOLING_FAILURE"),
    ),
)
def test_synthetic_process_distinguishes_assertions_from_collection_and_tooling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    return_code: int,
    report_element: str | None,
    failure_origin: str,
) -> None:
    def complete(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        report_argument = next(argument for argument in command if argument.startswith("--junitxml="))
        if report_element is not None:
            report_path = Path(report_argument.partition("=")[2])
            report_path.write_text(
                f"<testsuites><testsuite><testcase><{report_element}>failed"
                f"</{report_element}></testcase></testsuite></testsuites>",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(
            args=["pytest"],
            returncode=return_code,
            stdout="",
            stderr="failed",
        )

    monkeypatch.setattr(
        subprocess,
        "run",
        complete,
    )

    outcome = _run_synthetic_command(
        ["pytest"],
        harness=runner._SyntheticHarness.PYTEST,
        cwd=tmp_path,
        timeout_seconds=7,
        evidence_refs=("test-ref",),
        log_prefix="pytest:classified-failure",
    )

    assert outcome.status == "FAILED"
    assert outcome.failure_origin == failure_origin


def test_ui_runner_treats_browser_launch_failure_as_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend_root = tmp_path / "Frontend"
    frontend_root.mkdir()
    ui_scenario = synthetic_scenario(SyntheticScenarioId.EVIDENCE_CONTROLS_READ_ONLY)
    launch_failure_report = {
        "errors": [],
        "suites": [
            {
                "specs": [
                    {
                        "title": _UI_TEST_REF.rsplit("::", maxsplit=1)[-1],
                        "tests": [
                            {
                                "expectedStatus": "passed",
                                "results": [
                                    {
                                        "status": "failed",
                                        "attachments": [],
                                        "error": {"message": "browserType.launch: executable missing"},
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ],
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=list(_ui_test_command()),
            returncode=1,
            stdout=json.dumps(launch_failure_report),
            stderr="",
        ),
    )

    outcome = _run_synthetic_command(
        list(_ui_test_command()),
        harness=runner._SyntheticHarness.PLAYWRIGHT,
        cwd=frontend_root,
        timeout_seconds=BOUNDED_UI_CAMPAIGN.timeout_seconds,
        evidence_refs=ui_scenario.test_refs,
        log_prefix="playwright:launch-failure",
    )

    assert outcome.status == "FAILED"
    assert outcome.failure_origin == "INFRASTRUCTURE_OR_TOOLING_FAILURE"


def test_ui_runner_treats_structured_test_assertion_as_observed_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend_root = tmp_path / "Frontend"
    frontend_root.mkdir()
    ui_scenario = synthetic_scenario(SyntheticScenarioId.EVIDENCE_CONTROLS_READ_ONLY)
    assertion_failure_report = {
        "errors": [],
        "suites": [
            {
                "specs": [
                    {
                        "title": _UI_TEST_REF.rsplit("::", maxsplit=1)[-1],
                        "tests": [
                            {
                                "expectedStatus": "passed",
                                "results": [
                                    {
                                        "status": "failed",
                                        "attachments": [
                                            {
                                                "name": ("qualification-acceptance-assertion-failed"),
                                                "contentType": "application/json",
                                            }
                                        ],
                                        "error": {"message": "expect(locator).toBeVisible() failed"},
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ],
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=list(_ui_test_command()),
            returncode=1,
            stdout=json.dumps(assertion_failure_report),
            stderr="",
        ),
    )

    outcome = _run_synthetic_command(
        list(_ui_test_command()),
        harness=runner._SyntheticHarness.PLAYWRIGHT,
        cwd=frontend_root,
        timeout_seconds=BOUNDED_UI_CAMPAIGN.timeout_seconds,
        evidence_refs=ui_scenario.test_refs,
        log_prefix="playwright:assertion-failure",
    )

    assert outcome.status == "FAILED"
    assert outcome.failure_origin == "OBSERVED_INVARIANT_FAILURE"


@pytest.mark.parametrize("stdout", ("", "not-json"))
def test_ui_runner_treats_missing_or_malformed_report_as_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
) -> None:
    frontend_root = tmp_path / "Frontend"
    frontend_root.mkdir()
    ui_scenario = synthetic_scenario(SyntheticScenarioId.EVIDENCE_CONTROLS_READ_ONLY)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=list(_ui_test_command()),
            returncode=1,
            stdout=stdout,
            stderr="playwright did not emit a structured report",
        ),
    )

    outcome = _run_synthetic_command(
        list(_ui_test_command()),
        harness=runner._SyntheticHarness.PLAYWRIGHT,
        cwd=frontend_root,
        timeout_seconds=BOUNDED_UI_CAMPAIGN.timeout_seconds,
        evidence_refs=ui_scenario.test_refs,
        log_prefix="playwright:malformed-report",
    )

    assert outcome.status == "FAILED"
    assert outcome.failure_origin == "INFRASTRUCTURE_OR_TOOLING_FAILURE"


def test_invalid_preverified_ui_evidence_is_an_infrastructure_failure(
    tmp_path: Path,
) -> None:
    ui_scenario = next(
        scenario for scenario in SYNTHETIC_REHEARSAL_SCENARIOS if scenario.category is SyntheticScenarioCategory.UI
    )
    receipt = tmp_path / "invalid-ui-evidence.json"
    receipt.write_text("{}", encoding="utf-8")

    outcome = _run_synthetic_scenario(
        ui_scenario,
        repository_root=tmp_path,
        service_root=tmp_path,
        preverified_ui_evidence=receipt,
    )

    assert outcome.status == "FAILED"
    assert outcome.failure_origin == "INFRASTRUCTURE_OR_TOOLING_FAILURE"


def test_failed_process_summary_is_bounded_for_the_report_schema() -> None:
    completed = subprocess.CompletedProcess(
        args=["qualification-test"],
        returncode=1,
        stdout="x" * 8_000,
        stderr="y" * 8_000,
    )

    detail = _bounded_process_failure(completed)

    assert len(detail) == _MAX_FAILURE_DETAIL_CHARS
    assert detail.startswith("return code 1;")


def test_core_phase_failure_writes_classified_abort_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner,
        "_run_synthetic_scenario",
        lambda *args, **kwargs: runner.SyntheticTestOutcome(
            status="PASSED",
            evidence_refs=("bounded-test",),
        ),
    )

    def fail_core(**kwargs: object) -> object:
        del kwargs
        cause = RuntimeError("recovery hash mismatch")
        raise SyntheticRehearsalPhaseError(
            abort_cause=SyntheticAbortCause.RECOVERY_HASH_IDENTITY_FAILURE,
            phase="RECOVERY_INTEGRITY",
            cause=cause,
        )

    monkeypatch.setattr(runner, "run_synthetic_custody_rehearsal", fail_core)
    json_output = tmp_path / "abort.json"
    markdown_output = tmp_path / "abort.md"
    args = Namespace(
        artifacts_root=tmp_path,
        production_artifacts_root=tmp_path / "production",
        fixture_dir=tmp_path,
        production_account_id="PRODUCTION-G1",
        preverified_ui_evidence=None,
        json_output=json_output,
        markdown_output=markdown_output,
    )

    return_code = _run_synthetic_rehearsal(args)
    payload = json.loads(json_output.read_text(encoding="utf-8"))

    assert return_code == 1
    assert payload["abort_class"] == "CUSTODY_ABORT"
    assert payload["abort_cause"] == "RECOVERY_HASH_IDENTITY_FAILURE"
    assert payload["phase"] == "RECOVERY_INTEGRITY"
    assert payload["live_environment_status"] == "NOT_RUN"
    assert "ABORTED synthetic evidence only" in markdown_output.read_text(encoding="utf-8")


def test_compose_qualification_cannot_write_production_authority() -> None:
    repository_root = Path(__file__).parents[3]
    compose = yaml.safe_load((repository_root / "compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["alpaca-clerk-qualification"]
    mounts = {
        target: (source, frozenset(options.split(",")) if options else frozenset())
        for source, target, options in ((*volume.rsplit(":", maxsplit=2), "")[:3] for volume in service["volumes"])
    }

    assert mounts["/app/production-alpaca-clerk"] == (
        "alpaca-clerk-data",
        frozenset({"ro"}),
    )
    assert mounts["/app/artifacts/alpaca_clerk/qualification"][0] == ("alpaca-clerk-qualification-data")
    assert Path("/app/production-alpaca-clerk") == PRODUCTION_ARTIFACTS_ROOT
    assert Path("/app/artifacts/alpaca_clerk/qualification") == (QUALIFICATION_ARTIFACTS_ROOT)
    assert mounts["/app/contracts/alpaca-clerk-ui-correlation-campaign.v4.json"] == (
        "./contracts/alpaca-clerk-ui-correlation-campaign.v4.json",
        frozenset({"ro", "z"}),
    )
    assert all(
        not (source == "alpaca-clerk-data" and target.startswith("/app/artifacts/alpaca_clerk"))
        for target, (source, _options) in mounts.items()
    )
    assert "alpaca-clerk-qualification-data" in compose["volumes"]
    assert service["network_mode"] == "none"
    environment = set(service["environment"])
    assert any(item.startswith("ALPACA_QUALIFICATION_API_KEY_ID=") for item in environment)
    assert any(item.startswith("ALPACA_QUALIFICATION_API_SECRET_KEY=") for item in environment)
    assert not any(item.startswith("ALPACA_API_KEY_ID=") for item in environment)
    assert not any(item.startswith("ALPACA_API_SECRET_KEY=") for item in environment)
    assert any(
        source.startswith("${ALPACA_CLERK_UI_EVIDENCE_PATH:-")
        and target == "/app/evidence/ui.json"
        and {"ro", "z"}.issubset(options)
        for target, (source, options) in mounts.items()
    )
