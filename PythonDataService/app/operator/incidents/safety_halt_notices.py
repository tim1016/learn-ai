"""OperatorIncident builder for poisoned safety halts."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.engine.live.halt import (
    POISONED_FLAG_FILENAME,
    PoisonedHaltReason,
    read_poisoned_flag,
    write_poisoned_flag,
)
from app.operator.incidents.store import IncidentStore
from app.operator.notices.schema import (
    OperatorIncident,
    OperatorNotice,
    OperatorNoticeAction,
)


def safety_halt_incident_id(
    *,
    strategy_instance_id: str,
    run_id: str,
    halt_reason: PoisonedHaltReason,
    artifact_path: Path | None,
) -> str:
    """Stable id for one poisoned safety-halt story."""

    key = {
        "strategy_instance_id": strategy_instance_id,
        "run_id": run_id,
        "halt_trigger": halt_reason.trigger.value,
        "evidence_time_ms": halt_reason.halted_at_ms,
        "last_exit_artifact_ref": str(artifact_path) if artifact_path is not None else None,
    }
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"safety-halt-{digest}"


def build_safety_halt_incident(
    *,
    strategy_instance_id: str,
    run_id: str,
    halt_reason: PoisonedHaltReason,
    artifact_path: Path | None,
    log_path: Path | None,
) -> OperatorIncident:
    """Build the durable incident that makes poisoned halts visible."""

    trigger = halt_reason.trigger.value
    source = str(halt_reason.details.get("source") or "poisoned.flag")
    reason = str(halt_reason.details.get("reason") or trigger)
    evidence = {
        "strategy_instance_id": strategy_instance_id,
        "run_id": run_id,
        "halt_trigger": trigger,
        "source_flag": "poisoned.flag",
        "source": source,
        "evidence_time_ms": halt_reason.halted_at_ms,
        "last_clean_bar_close_ms": halt_reason.last_clean_bar_close_ms,
        "artifact_path": str(artifact_path) if artifact_path is not None else None,
        "log_path": str(log_path) if log_path is not None else None,
        "halt_details": dict(halt_reason.details),
    }
    return OperatorIncident(
        incident_id=safety_halt_incident_id(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            halt_reason=halt_reason,
            artifact_path=artifact_path,
        ),
        category="safety-halt",
        notice=OperatorNotice(
            code="safety_halt.poisoned",
            tier="critical",
            title="Safety halt recorded",
            message=(
                f"This run wrote poisoned.flag for {trigger}. "
                f"Reason: {reason}. Review the halt evidence before starting a fresh run."
            ),
            source_codes=[trigger],
            forensic_facts={
                "strategy_instance_id": strategy_instance_id,
                "run_id": run_id,
                "halt_trigger": trigger,
                "source": source,
                "evidence_time_ms": halt_reason.halted_at_ms,
                "artifact_path": str(artifact_path) if artifact_path is not None else None,
                "log_path": str(log_path) if log_path is not None else None,
            },
            actionability="actuatable",
            resolution="Clears when the poisoned run remains retired and the operator deploys a fresh run after reviewing halt evidence.",
            action=OperatorNoticeAction(kind="redeploy", label="Open Fresh run"),
            runbook_slug="safety-halt",
            occurred_at_ms=halt_reason.halted_at_ms,
        ),
        started_at_ms=halt_reason.halted_at_ms,
        evidence=evidence,
    )


@dataclass(frozen=True)
class PoisonedIncidentRecordResult:
    artifact_path: Path
    halt_reason: PoisonedHaltReason
    flag_created: bool


def poison_and_record_incident(
    *,
    run_dir: Path,
    halt_reason: PoisonedHaltReason,
    strategy_instance_id: str,
    run_id: str,
    log_path: Path | None,
    logger: logging.Logger,
) -> PoisonedIncidentRecordResult:
    """Write ``poisoned.flag`` and persist the operator incident.

    Existing flags keep the first halt's reason. If that existing flag is
    corrupted, log it loudly and use the current halt reason for the incident;
    the flag's presence still keeps the run poisoned at the start gate.
    """

    artifact_path = run_dir / POISONED_FLAG_FILENAME
    incident_reason = halt_reason
    flag_created = False
    try:
        artifact_path = write_poisoned_flag(run_dir, halt_reason)
        flag_created = True
    except FileExistsError:
        try:
            existing_reason = read_poisoned_flag(run_dir)
        except ValueError:
            logger.exception(
                "could not parse existing poisoned.flag for safety-halt incident",
                extra={"run_dir": str(run_dir)},
            )
        else:
            if existing_reason is not None:
                incident_reason = existing_reason

    try:
        IncidentStore(run_dir).append_unless_resolved(
            build_safety_halt_incident(
                strategy_instance_id=strategy_instance_id,
                run_id=run_id,
                halt_reason=incident_reason,
                artifact_path=artifact_path,
                log_path=log_path,
            )
        )
    except Exception:
        logger.exception(
            "could not record safety-halt operator incident",
            extra={
                "run_dir": str(run_dir),
                "strategy_instance_id": strategy_instance_id,
                "run_id": run_id,
            },
        )
    return PoisonedIncidentRecordResult(
        artifact_path=artifact_path,
        halt_reason=incident_reason,
        flag_created=flag_created,
    )
