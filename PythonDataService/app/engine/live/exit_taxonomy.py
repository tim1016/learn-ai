"""Closed exit taxonomy for live-run process retirement evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

RunExitCategory = Literal[
    "clean",
    "controlled_stop",
    "halted",
    "crashed",
    "ended_without_status",
    "poisoned",
    "recovery_flatten",
    "interrupted",
    "unclassified",
]

RUN_STATUS_FILENAME = "run_status.json"

_NON_CRASH_EXIT_REASONS: dict[str, tuple[RunExitCategory, str]] = {
    "normal": ("clean", "host_daemon.process_exited"),
    "force_flat_complete": ("clean", "host_daemon.process_exited"),
    "fatal_halt": ("halted", "host_daemon.process_halted"),
    "max_orders_exceeded": ("halted", "host_daemon.process_halted"),
    "keyboard_interrupt": ("interrupted", "host_daemon.process_stopped"),
    "signal": ("interrupted", "host_daemon.process_stopped"),
    "recovery_flatten": ("recovery_flatten", "host_daemon.recovery_flatten"),
    "poisoned": ("poisoned", "host_daemon.process_poisoned"),
}


@dataclass(frozen=True)
class RunExitEvidence:
    """Run-status fields needed to classify a terminal child process."""

    status_present: bool
    run_id: str | None = None
    exit_code: int | None = None
    exit_reason: str | None = None


@dataclass(frozen=True)
class RunExitTaxonomyVerdict:
    """Closed process-exit verdict used by daemon writes and repair jobs."""

    category: RunExitCategory
    registry_source: str


def read_run_exit_evidence(run_dir: Path) -> RunExitEvidence:
    """Read the minimal exit evidence from ``run_status.json``.

    Missing, corrupt, or non-object status files intentionally return
    ``status_present=False``. The historical backfill only repairs rows when the
    status file positively disproves a crash.
    """

    path = run_dir / RUN_STATUS_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return RunExitEvidence(status_present=False)
    if not isinstance(payload, dict):
        return RunExitEvidence(status_present=False)

    run_id = payload.get("run_id")
    exit_reason = payload.get("exit_reason")
    exit_code = payload.get("exit_code")
    return RunExitEvidence(
        status_present=True,
        run_id=run_id if isinstance(run_id, str) else None,
        exit_code=exit_code if isinstance(exit_code, int) else None,
        exit_reason=exit_reason if isinstance(exit_reason, str) else None,
    )


def classify_run_exit(
    evidence: RunExitEvidence,
    *,
    returncode: int | None,
    stopping: bool,
) -> RunExitTaxonomyVerdict:
    """Classify a terminal child process into the account-registry vocabulary."""

    if stopping:
        return RunExitTaxonomyVerdict(
            category="controlled_stop",
            registry_source="host_daemon.stop_exited",
        )

    code = evidence.exit_code if evidence.exit_code is not None else returncode
    if evidence.exit_reason in _NON_CRASH_EXIT_REASONS:
        category, source = _NON_CRASH_EXIT_REASONS[evidence.exit_reason]
        return RunExitTaxonomyVerdict(category=category, registry_source=source)
    if evidence.exit_reason == "exception":
        return RunExitTaxonomyVerdict(
            category="crashed",
            registry_source="host_daemon.process_crashed",
        )
    if evidence.exit_reason is None and code == 0:
        return RunExitTaxonomyVerdict(
            category="clean",
            registry_source="host_daemon.process_exited",
        )
    if evidence.exit_reason is None:
        return RunExitTaxonomyVerdict(
            category="ended_without_status",
            registry_source="host_daemon.ended_without_status",
        )
    if code is not None and code != 0:
        return RunExitTaxonomyVerdict(
            category="crashed",
            registry_source="host_daemon.process_crashed",
        )
    return RunExitTaxonomyVerdict(
        category="unclassified",
        registry_source="host_daemon.process_exited",
    )


def false_crash_repair_source(evidence: RunExitEvidence) -> str | None:
    """Return a replacement source when status evidence disproves a crash."""

    if not evidence.status_present or evidence.exit_reason is None:
        return None
    verdict = classify_run_exit(evidence, returncode=evidence.exit_code, stopping=False)
    if verdict.registry_source == "host_daemon.process_crashed":
        return None
    return verdict.registry_source


__all__ = [
    "RUN_STATUS_FILENAME",
    "RunExitEvidence",
    "RunExitTaxonomyVerdict",
    "classify_run_exit",
    "false_crash_repair_source",
    "read_run_exit_evidence",
]
