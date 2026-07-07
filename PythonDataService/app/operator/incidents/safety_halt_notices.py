"""OperatorIncident builder for poisoned safety halts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.engine.live.halt import PoisonedHaltReason
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
            action=OperatorNoticeAction(kind="redeploy", label="Open Fresh run"),
            runbook_slug="safety-halt",
            occurred_at_ms=halt_reason.halted_at_ms,
        ),
        started_at_ms=halt_reason.halted_at_ms,
        evidence=evidence,
    )
