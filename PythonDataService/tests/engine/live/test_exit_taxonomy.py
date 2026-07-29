"""Tests for the closed live-run exit taxonomy."""

from __future__ import annotations

from app.engine.live.exit_taxonomy import (
    ENDED_WITHOUT_STATUS_REGISTRY_SOURCE,
    LIVENESS_UNPROVEN_REGISTRY_SOURCE,
    PROCESS_CRASHED_REGISTRY_SOURCE,
    RunExitEvidence,
    classify_run_exit,
    false_crash_repair_source,
    terminal_restart_failure_phrase,
)


def test_exit_taxonomy_separates_halt_crash_and_missing_status() -> None:
    fatal_halt = classify_run_exit(
        RunExitEvidence(status_present=True, exit_code=1, exit_reason="fatal_halt"),
        returncode=1,
        stopping=False,
    )
    exception = classify_run_exit(
        RunExitEvidence(status_present=True, exit_code=3, exit_reason="exception"),
        returncode=3,
        stopping=False,
    )
    missing_status = classify_run_exit(
        RunExitEvidence(status_present=False),
        returncode=-9,
        stopping=False,
    )
    missing_status_zero = classify_run_exit(
        RunExitEvidence(status_present=False),
        returncode=0,
        stopping=False,
    )

    assert fatal_halt.registry_source == "host_daemon.process_halted"
    assert exception.registry_source == "host_daemon.process_crashed"
    assert missing_status.registry_source == "host_daemon.ended_without_status"
    assert missing_status_zero.registry_source == "host_daemon.ended_without_status"


def test_exit_taxonomy_keeps_unclassified_statuses_ambiguous() -> None:
    unclassified = classify_run_exit(
        RunExitEvidence(status_present=True, exit_code=0, exit_reason="legacy_unknown"),
        returncode=0,
        stopping=False,
    )

    assert unclassified.category == "unclassified"
    assert unclassified.registry_source == "host_daemon.ended_without_status"


def test_false_crash_repair_requires_positive_non_crash_status() -> None:
    assert (
        false_crash_repair_source(
            RunExitEvidence(status_present=True, exit_code=1, exit_reason="fatal_halt")
        )
        == "host_daemon.process_halted"
    )
    assert (
        false_crash_repair_source(
            RunExitEvidence(status_present=True, exit_code=3, exit_reason="exception")
        )
        is None
    )
    assert false_crash_repair_source(RunExitEvidence(status_present=False)) is None
    assert (
        false_crash_repair_source(
            RunExitEvidence(status_present=True, exit_code=0, exit_reason="legacy_unknown")
        )
        is None
    )


def test_terminal_restart_failure_phrase_is_accurate_per_source() -> None:
    """The operator message must not label an OOM/SIGKILL exit as a 'crash'."""

    assert (
        terminal_restart_failure_phrase(LIVENESS_UNPROVEN_REGISTRY_SOURCE)
        == "is not owned by the current host daemon and its liveness is unproven"
    )
    assert (
        terminal_restart_failure_phrase(ENDED_WITHOUT_STATUS_REGISTRY_SOURCE)
        == "ended without a run-status receipt"
    )
    assert terminal_restart_failure_phrase(PROCESS_CRASHED_REGISTRY_SOURCE) == "crashed"
