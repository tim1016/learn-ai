"""Write deterministic pre-live evidence for the SQLite manual-order release gate.

This command intentionally executes only broker-free regressions.  A passing
report proves the deterministic release matrix, not that a paper-account
ceremony happened; the feature flag must remain disabled until the separate
dated paper receipt is archived.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.sqlite.operational_files import atomic_write_json, atomic_write_text
from app.services.manual_order_qualification import (
    MANUAL_QUALIFICATION_SCENARIOS,
    ManualQualificationScenario,
)

_SCHEMA_VERSION = 1
_PYTEST_TIMEOUT_SECONDS = 300
_MAX_FAILURE_DETAIL_CHARS = 2_000


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.run_manual_order_qualification",
        description="Run broker-free SQLite manual-order pre-live qualification evidence.",
    )
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--markdown-output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.json_output.resolve() == args.markdown_output.resolve():
        parser.error("--json-output and --markdown-output must be different files")
    return args


def _run_scenario(
    scenario: ManualQualificationScenario,
    *,
    working_directory: Path,
    now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
) -> dict[str, Any]:
    started_at_ms = now_ms()
    command = [sys.executable, "-m", "pytest", "-q", *scenario.test_refs]
    try:
        completed = subprocess.run(
            command,
            cwd=working_directory,
            check=False,
            capture_output=True,
            text=True,
            timeout=_PYTEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        detail = f"pytest exceeded {_PYTEST_TIMEOUT_SECONDS}s: {_stream_tail(exc.stderr)}"
        return _scenario_result(
            scenario,
            status="FAILED",
            started_at_ms=started_at_ms,
            completed_at_ms=now_ms(),
            failure_detail=detail,
        )
    except OSError as exc:
        return _scenario_result(
            scenario,
            status="FAILED",
            started_at_ms=started_at_ms,
            completed_at_ms=now_ms(),
            failure_detail=f"could not execute pytest: {type(exc).__name__}: {exc}",
        )
    detail = None if completed.returncode == 0 else _failure_detail(completed)
    return _scenario_result(
        scenario,
        status="PASSED" if completed.returncode == 0 else "FAILED",
        started_at_ms=started_at_ms,
        completed_at_ms=now_ms(),
        failure_detail=detail,
        stdout_sha256=_sha256(completed.stdout),
        stderr_sha256=_sha256(completed.stderr),
    )


def _scenario_result(
    scenario: ManualQualificationScenario,
    *,
    status: str,
    started_at_ms: int,
    completed_at_ms: int,
    failure_detail: str | None,
    stdout_sha256: str | None = None,
    stderr_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "scenario_id": scenario.scenario_id,
        "title": scenario.title,
        "test_refs": list(scenario.test_refs),
        "status": status,
        "started_at_ms": started_at_ms,
        "completed_at_ms": completed_at_ms,
        "stdout_sha256": stdout_sha256,
        "stderr_sha256": stderr_sha256,
        "failure_detail": failure_detail[:_MAX_FAILURE_DETAIL_CHARS] if failure_detail else None,
    }


def _report(*, scenarios: list[dict[str, Any]], generated_at_ms: int) -> dict[str, Any]:
    passed = all(scenario["status"] == "PASSED" for scenario in scenarios)
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "label": "SQLITE_MANUAL_ORDER_PRE_LIVE_QUALIFICATION",
        "generated_at_ms": generated_at_ms,
        "overall_status": "PRE_LIVE_REHEARSAL_PASSED" if passed else "PRE_LIVE_REHEARSAL_FAILED",
        "live_environment_status": "NOT_RUN",
        "release_gate_status": "PENDING_DATED_PAPER_CEREMONY",
        "scenarios": scenarios,
    }
    payload["report_sha256"] = _sha256(_canonical_json(payload))
    return payload


def _markdown(report: dict[str, Any]) -> str:
    lines = (
        "## SQLite manual-order pre-live qualification",
        "",
        f"- Overall: `{report['overall_status']}`",
        f"- Live paper ceremony: `{report['live_environment_status']}`",
        f"- Release gate: `{report['release_gate_status']}`",
        f"- Report SHA-256: `{report['report_sha256']}`",
        "",
        "| Scenario | Status | Focused regressions |",
        "| --- | --- | ---: |",
        *(
            f"| {scenario['scenario_id']} | `{scenario['status']}` | {len(scenario['test_refs'])} |"
            for scenario in report["scenarios"]
        ),
        "",
        "A passing pre-live report does not authorize the feature flag. Archive a dated paper-account "
        "ceremony receipt covering submit, fill, reduce, cancel, restart recovery, reconciliation, "
        "and post-terminal bot admission before enabling manual trading.",
        "",
    )
    return "\n".join(lines)


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stream_tail(value: str | bytes | None) -> str:
    if value is None:
        return "<empty>"
    if isinstance(value, bytes):
        value = value.decode(encoding="utf-8", errors="replace")
    return value[-1_000:].strip() or "<empty>"


def _failure_detail(completed: subprocess.CompletedProcess[str]) -> str:
    return (
        f"pytest returned {completed.returncode}; stdout tail: {_stream_tail(completed.stdout)}; "
        f"stderr tail: {_stream_tail(completed.stderr)}"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    working_directory = Path(__file__).resolve().parents[1]
    scenarios = [
        _run_scenario(scenario, working_directory=working_directory)
        for scenario in MANUAL_QUALIFICATION_SCENARIOS
    ]
    report = _report(scenarios=scenarios, generated_at_ms=time.time_ns() // 1_000_000)
    atomic_write_json(args.json_output, report)
    atomic_write_text(args.markdown_output, _markdown(report))
    sys.stdout.write(
        json.dumps(
            {
                "overall_status": report["overall_status"],
                "release_gate_status": report["release_gate_status"],
                "report_sha256": report["report_sha256"],
                "scenario_count": len(scenarios),
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if report["overall_status"] == "PRE_LIVE_REHEARSAL_PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
