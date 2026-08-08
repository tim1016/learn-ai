"""Run deterministic Alpaca SQLite Clerk adversarial and scale qualification."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as element_tree
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.sqlite.operational_files import (
    atomic_write_json,
    atomic_write_text,
)
from app.broker.alpaca.clerk.sqlite.qualification import (
    SMOKE_ADVERSARIAL_TESTS,
    SyntheticRehearsalPhaseError,
    SyntheticTestOutcome,
    qualification_markdown,
    run_performance_profile,
    run_synthetic_custody_rehearsal,
    synthetic_rehearsal_markdown,
)
from app.broker.alpaca.clerk.sqlite.qualification_synthetic_rehearsal import (
    SyntheticTestFailureOrigin,
)
from app.broker.alpaca.clerk.sqlite.qualification_ui_evidence import (
    BOUNDED_UI_CAMPAIGN,
)
from app.broker.alpaca.clerk.sqlite.qualification_ui_evidence import (
    load_preverified_ui_outcome as _load_preverified_ui_outcome,
)
from app.schemas.account_custody_qualification import (
    account_custody_qualification_payload_sha256,
)
from app.schemas.account_custody_synthetic_qualification import (
    SyntheticHarnessAbortReport,
)
from app.services.account_custody_synthetic_scenarios import (
    SYNTHETIC_REHEARSAL_SCENARIOS,
    SyntheticRehearsalScenario,
    SyntheticScenarioCategory,
)

_UI_SOURCE_RELATIVE = Path("Frontend/tests/e2e/alpaca-clerk-ui-correlation.spec.ts")
_CONTAINER_UI_SOURCE = Path("/app/evidence/ui-spec.ts")
_MAX_FAILURE_DETAIL_CHARS = 2_000
_PROFILE_PYTEST_TIMEOUT_SECONDS = 900
_SCENARIO_PYTEST_TIMEOUT_SECONDS = 300
_UI_ASSERTION_FAILURE_ATTACHMENT = "qualification-acceptance-assertion-failed"


class _SyntheticHarness(StrEnum):
    PYTEST = "PYTEST"
    PLAYWRIGHT = "PLAYWRIGHT"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.run_alpaca_sqlite_qualification",
        description="Run broker-free SQLite Clerk qualification evidence.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--profile", choices=("smoke", "full"))
    mode.add_argument(
        "--synthetic-rehearsal",
        action="store_true",
        help="Run the #1415 pre-live rehearsal without contacting a live broker.",
    )
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--artifacts-root", type=Path)
    parser.add_argument("--production-artifacts-root", type=Path)
    parser.add_argument("--fixture-dir", type=Path)
    parser.add_argument("--production-account-id")
    parser.add_argument(
        "--preverified-ui-evidence",
        type=Path,
        help=(
            "Bounded JSON receipt for the exact focused Angular regression. "
            "Accepted only when the Frontend checkout is unavailable."
        ),
    )
    parser.add_argument(
        "--skip-adversarial",
        action="store_true",
        help="Performance-only developer iteration; never use for qualification evidence.",
    )
    args = parser.parse_args(argv)
    if args.synthetic_rehearsal:
        required = {
            "--artifacts-root": args.artifacts_root,
            "--production-artifacts-root": args.production_artifacts_root,
            "--fixture-dir": args.fixture_dir,
            "--production-account-id": args.production_account_id,
        }
        missing = tuple(option for option, value in required.items() if value is None)
        if missing:
            parser.error(f"synthetic rehearsal requires {', '.join(missing)}")
        if not args.production_account_id.strip() or len(args.production_account_id) > 64:
            parser.error("--production-account-id must contain between 1 and 64 characters")
        if args.skip_adversarial:
            parser.error("--skip-adversarial is available only with --profile")
    else:
        synthetic_options = {
            "--artifacts-root": args.artifacts_root,
            "--production-artifacts-root": args.production_artifacts_root,
            "--fixture-dir": args.fixture_dir,
            "--production-account-id": args.production_account_id,
            "--preverified-ui-evidence": args.preverified_ui_evidence,
        }
        supplied = tuple(option for option, value in synthetic_options.items() if value is not None)
        if supplied:
            parser.error(f"{', '.join(supplied)} require --synthetic-rehearsal")
    if args.json_output.resolve() == args.markdown_output.resolve():
        parser.error("--json-output and --markdown-output must be different files")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.synthetic_rehearsal:
        return _run_synthetic_rehearsal(args)

    report = run_performance_profile(args.profile)
    if args.skip_adversarial:
        adversarial = {
            "status": "SKIPPED",
            "reason": "explicit --skip-adversarial developer iteration",
            "tests": [],
        }
    else:
        tests = (
            SMOKE_ADVERSARIAL_TESTS
            if args.profile == "smoke"
            else ("tests/broker/alpaca/clerk/sqlite", "tests/broker/alpaca/test_trade_updates.py")
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", *tests],
                check=False,
                capture_output=True,
                text=True,
                timeout=_PROFILE_PYTEST_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            adversarial = {
                "status": "FAILED",
                "tests": list(tests),
                "failure_detail": _bounded_process_exception(
                    exc,
                    message=(f"adversarial pytest exceeded the bounded {_PROFILE_PYTEST_TIMEOUT_SECONDS}s timeout"),
                ),
            }
        except OSError as exc:
            adversarial = {
                "status": "FAILED",
                "tests": list(tests),
                "failure_detail": _bounded_os_error(exc),
            }
        else:
            adversarial = {
                "status": "PASSED" if completed.returncode == 0 else "FAILED",
                "return_code": completed.returncode,
                "tests": list(tests),
                "stdout_tail": completed.stdout[-8_000:],
                "stderr_tail": completed.stderr[-8_000:],
            }
    report["adversarial"] = adversarial
    atomic_write_json(args.json_output, report)
    markdown = qualification_markdown(report) + "\n" + _adversarial_markdown(adversarial)
    atomic_write_text(args.markdown_output, markdown)
    sys.stdout.write(json.dumps(_summary(report), sort_keys=True) + "\n")
    passed = adversarial["status"] in {"PASSED", "SKIPPED"} and report["performance_budget"]["status"] == "PASSED"
    return 0 if passed else 1


def _run_synthetic_rehearsal(args: argparse.Namespace) -> int:
    service_root = Path(__file__).resolve().parents[1]
    repository_root = service_root.parent
    outcomes = {
        scenario.scenario_id: _run_synthetic_scenario(
            scenario,
            repository_root=repository_root,
            service_root=service_root,
            preverified_ui_evidence=args.preverified_ui_evidence,
        )
        for scenario in SYNTHETIC_REHEARSAL_SCENARIOS
    }
    try:
        report = run_synthetic_custody_rehearsal(
            artifacts_root=args.artifacts_root,
            production_artifacts_root=args.production_artifacts_root,
            fixture_directory=args.fixture_dir,
            production_account_id=args.production_account_id,
            test_outcomes=outcomes,
        )
    except SyntheticRehearsalPhaseError as exc:
        abort_payload: dict[str, Any] = {
            "schema_version": 1,
            "generated_at_ms": time.time_ns() // 1_000_000,
            "label": "PRE_LIVE_REHEARSAL_SYNTHETIC_ABORT",
            "live_environment_status": "NOT_RUN",
            "adr_0035_status": "PROPOSED",
            "overall_status": "ABORTED",
            "abort_class": exc.abort_class,
            "abort_cause": exc.abort_cause,
            "phase": exc.phase,
            "failure_type": type(exc.cause).__name__,
            "failure_detail": str(exc.cause)[:_MAX_FAILURE_DETAIL_CHARS] or "core phase failed without a message",
        }
        abort_payload["report_sha256"] = account_custody_qualification_payload_sha256(abort_payload)
        abort_report = SyntheticHarnessAbortReport.model_validate(abort_payload)
        file_sha256 = atomic_write_json(
            args.json_output,
            abort_report.model_dump(mode="json"),
        )
        atomic_write_text(
            args.markdown_output,
            _synthetic_abort_markdown(abort_report),
        )
        sys.stdout.write(
            json.dumps(
                {
                    "abort_class": abort_report.abort_class,
                    "abort_cause": abort_report.abort_cause,
                    "file_sha256": file_sha256,
                    "label": abort_report.label,
                    "overall_status": abort_report.overall_status,
                    "phase": abort_report.phase,
                    "report_sha256": abort_report.report_sha256,
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 1
    json_sha256 = atomic_write_json(
        args.json_output,
        report.model_dump(mode="json"),
    )
    atomic_write_text(args.markdown_output, synthetic_rehearsal_markdown(report))
    sys.stdout.write(
        json.dumps(
            {
                "abort_class": report.abort_class,
                "file_sha256": json_sha256,
                "label": report.label,
                "overall_status": report.overall_status,
                "report_sha256": report.report_sha256,
                "scenario_count": len(report.scenarios),
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if report.overall_status != "ABORTED" else 1


def _synthetic_abort_markdown(report: SyntheticHarnessAbortReport) -> str:
    return "\n".join(
        (
            "## Pre-live rehearsal (synthetic)",
            "",
            "> **ABORTED synthetic evidence only.** No live scenario was run or "
            "checked off. ADR 0035 remains **Proposed**.",
            "",
            f"- Report SHA-256: `{report.report_sha256}`",
            f"- Abort classification: `{report.abort_class}`",
            f"- Abort cause: `{report.abort_cause}`",
            f"- Failed phase: `{report.phase}`",
            f"- Failure: `{report.failure_type}` — {report.failure_detail}",
            "",
        )
    )


def _run_synthetic_scenario(
    scenario: SyntheticRehearsalScenario,
    *,
    repository_root: Path,
    service_root: Path,
    preverified_ui_evidence: Path | None,
) -> SyntheticTestOutcome:
    if scenario.category is SyntheticScenarioCategory.UI:
        if preverified_ui_evidence is not None:
            try:
                ui_source_path = repository_root / _UI_SOURCE_RELATIVE
                if not ui_source_path.is_file():
                    ui_source_path = _CONTAINER_UI_SOURCE
                return _load_preverified_ui_outcome(
                    preverified_ui_evidence,
                    ui_source_path=ui_source_path,
                    checkout_root=(repository_root if (repository_root / ".git").exists() else None),
                    expected_git_commit_sha=os.environ.get("GIT_SHA"),
                )
            except (OSError, ValueError) as exc:
                return SyntheticTestOutcome(
                    status="FAILED",
                    evidence_refs=scenario.test_refs,
                    log_refs=("runner:invalid-preverified-ui-evidence",),
                    failure_detail=str(exc)[:_MAX_FAILURE_DETAIL_CHARS],
                    failure_origin=(SyntheticTestFailureOrigin.INFRASTRUCTURE_OR_TOOLING_FAILURE),
                )
        return SyntheticTestOutcome(
            status="FAILED",
            evidence_refs=scenario.test_refs,
            log_refs=("runner:frontend-checkout-unavailable",),
            failure_detail=(
                "--preverified-ui-evidence is required so a successful browser "
                "campaign includes authenticated correlation measurements"
            ),
            failure_origin=(SyntheticTestFailureOrigin.INFRASTRUCTURE_OR_TOOLING_FAILURE),
        )

    pytest_refs = tuple(ref for ref in scenario.test_refs if ref.startswith("tests/"))
    if pytest_refs != scenario.test_refs:
        if scenario.category is SyntheticScenarioCategory.RECOVERY and not pytest_refs:
            return SyntheticTestOutcome(
                status="PENDING_CORE_VALIDATION",
                evidence_refs=scenario.test_refs,
                log_refs=("runner:pending-protected-generation-recovery-ceremony",),
            )
        return SyntheticTestOutcome(
            status="FAILED",
            evidence_refs=scenario.test_refs,
            log_refs=(f"runner:{scenario.scenario_id}:invalid-test-reference",),
            failure_detail="scenario contains a test reference the Python runner cannot execute",
            failure_origin=(SyntheticTestFailureOrigin.INFRASTRUCTURE_OR_TOOLING_FAILURE),
        )
    return _run_synthetic_command(
        [sys.executable, "-m", "pytest", "-q", *pytest_refs],
        harness=_SyntheticHarness.PYTEST,
        cwd=service_root,
        timeout_seconds=_SCENARIO_PYTEST_TIMEOUT_SECONDS,
        evidence_refs=pytest_refs,
        log_prefix=f"pytest:{scenario.scenario_id}",
    )


def _ui_test_command() -> tuple[str, ...]:
    return BOUNDED_UI_CAMPAIGN.command


def _run_synthetic_command(
    command: list[str],
    *,
    harness: _SyntheticHarness,
    cwd: Path,
    timeout_seconds: int,
    evidence_refs: tuple[str, ...],
    log_prefix: str,
) -> SyntheticTestOutcome:
    started_at_ms = time.time_ns() // 1_000_000
    with tempfile.TemporaryDirectory(prefix="qualification-test-report-") as report_dir:
        pytest_report_path = Path(report_dir) / "pytest.xml" if harness is _SyntheticHarness.PYTEST else None
        executed_command = list(command)
        if pytest_report_path is not None:
            executed_command.append(f"--junitxml={pytest_report_path}")
        try:
            completed = subprocess.run(
                executed_command,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            completed_at_ms = time.time_ns() // 1_000_000
            return SyntheticTestOutcome(
                status="FAILED",
                evidence_refs=evidence_refs,
                log_refs=(f"{log_prefix}:timeout:{timeout_seconds}s",),
                failure_detail=_bounded_process_exception(
                    exc,
                    message=f"command exceeded the bounded {timeout_seconds}s timeout",
                ),
                failure_origin=(SyntheticTestFailureOrigin.INFRASTRUCTURE_OR_TOOLING_FAILURE),
                started_at_ms=started_at_ms,
                completed_at_ms=completed_at_ms,
            )
        except OSError as exc:
            completed_at_ms = time.time_ns() // 1_000_000
            return SyntheticTestOutcome(
                status="FAILED",
                evidence_refs=evidence_refs,
                log_refs=(f"{log_prefix}:os-error:{type(exc).__name__}",),
                failure_detail=_bounded_os_error(exc),
                failure_origin=(SyntheticTestFailureOrigin.INFRASTRUCTURE_OR_TOOLING_FAILURE),
                started_at_ms=started_at_ms,
                completed_at_ms=completed_at_ms,
            )
        completed_at_ms = time.time_ns() // 1_000_000
        return SyntheticTestOutcome(
            status="PASSED" if completed.returncode == 0 else "FAILED",
            evidence_refs=evidence_refs,
            log_refs=(f"{log_prefix}:return-code:{completed.returncode}",),
            failure_detail=(None if completed.returncode == 0 else _bounded_process_failure(completed)),
            failure_origin=(
                None
                if completed.returncode == 0
                else _classify_synthetic_failure(
                    harness,
                    completed,
                    evidence_refs=evidence_refs,
                    pytest_report_path=pytest_report_path,
                )
            ),
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
        )


def _classify_synthetic_failure(
    harness: _SyntheticHarness,
    completed: subprocess.CompletedProcess[str],
    *,
    evidence_refs: tuple[str, ...],
    pytest_report_path: Path | None,
) -> SyntheticTestFailureOrigin:
    if harness is _SyntheticHarness.PYTEST:
        if _pytest_report_contains_call_failure(pytest_report_path):
            return SyntheticTestFailureOrigin.OBSERVED_INVARIANT_FAILURE
        return SyntheticTestFailureOrigin.INFRASTRUCTURE_OR_TOOLING_FAILURE
    if _playwright_report_has_observed_failure(completed.stdout, evidence_refs):
        return SyntheticTestFailureOrigin.OBSERVED_INVARIANT_FAILURE
    return SyntheticTestFailureOrigin.INFRASTRUCTURE_OR_TOOLING_FAILURE


def _pytest_report_contains_call_failure(report_path: Path | None) -> bool:
    """Return true only for executed-test failures, never setup/collection errors."""

    if report_path is None or not report_path.is_file():
        return False
    try:
        root = element_tree.parse(report_path).getroot()
    except (element_tree.ParseError, OSError):
        return False
    if root.findall(".//error"):
        return False
    return bool(root.findall(".//failure"))


def _playwright_report_has_observed_failure(
    stdout: str,
    evidence_refs: tuple[str, ...],
) -> bool:
    if len(evidence_refs) != 1:
        return False
    try:
        report = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(report, dict) or report.get("errors") != []:
        return False

    expected_title = evidence_refs[0].rsplit("::", maxsplit=1)[-1]
    for spec in _playwright_specs(report.get("suites")):
        if spec.get("title") != expected_title:
            continue
        tests = spec.get("tests")
        if not isinstance(tests, list):
            continue
        for test in tests:
            if not isinstance(test, dict) or test.get("expectedStatus") != "passed":
                continue
            results = test.get("results")
            if not isinstance(results, list):
                continue
            for result in results:
                if not isinstance(result, dict) or result.get("status") != "failed":
                    continue
                attachments = result.get("attachments")
                if not isinstance(attachments, list):
                    continue
                if any(
                    isinstance(attachment, dict) and attachment.get("name") == _UI_ASSERTION_FAILURE_ATTACHMENT
                    for attachment in attachments
                ):
                    return True
    return False


def _playwright_specs(suites: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(suites, list):
        return ()
    specs: list[dict[str, Any]] = []
    for suite in suites:
        if not isinstance(suite, dict):
            continue
        suite_specs = suite.get("specs")
        if isinstance(suite_specs, list):
            specs.extend(spec for spec in suite_specs if isinstance(spec, dict))
        specs.extend(_playwright_specs(suite.get("suites")))
    return tuple(specs)


def _bounded_process_failure(completed: subprocess.CompletedProcess[str]) -> str:
    stdout_tail = completed.stdout[-4_000:].strip()
    stderr_tail = completed.stderr[-4_000:].strip()
    detail = (
        f"return code {completed.returncode}; "
        f"stdout tail: {stdout_tail or '<empty>'}; "
        f"stderr tail: {stderr_tail or '<empty>'}"
    )
    return detail[:_MAX_FAILURE_DETAIL_CHARS]


def _bounded_process_exception(
    exc: subprocess.TimeoutExpired,
    *,
    message: str,
) -> str:
    stdout_tail = _stream_tail(exc.stdout)
    stderr_tail = _stream_tail(exc.stderr)
    detail = f"{message}; stdout tail: {stdout_tail or '<empty>'}; stderr tail: {stderr_tail or '<empty>'}"
    return detail[:_MAX_FAILURE_DETAIL_CHARS]


def _stream_tail(stream: str | bytes | None) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        stream = stream.decode(encoding="utf-8", errors="replace")
    return stream[-4_000:].strip()


def _bounded_os_error(exc: OSError) -> str:
    return f"could not execute qualification command: {type(exc).__name__}: {exc}"[:_MAX_FAILURE_DETAIL_CHARS]


def _adversarial_markdown(adversarial: dict[str, Any]) -> str:
    return (
        "## Adversarial campaign\n\n"
        f"- Status: `{adversarial['status']}`\n"
        f"- Tests: `{len(adversarial['tests'])}` selected path(s)/node(s)\n"
    )


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile": report["profile"],
        "generated_at_ms": report["generated_at_ms"],
        "scale_count": len(report["scales"]),
        "adversarial_status": report["adversarial"]["status"],
        "performance_budget_status": report["performance_budget"]["status"],
    }


if __name__ == "__main__":
    sys.exit(main())
