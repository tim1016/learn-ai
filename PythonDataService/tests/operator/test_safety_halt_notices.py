from __future__ import annotations

from pathlib import Path

from app.engine.live.halt import PoisonedHaltReason, PoisonedHaltTrigger
from app.operator.incidents.safety_halt_notices import (
    build_safety_halt_incident,
    safety_halt_incident_id,
)


def _reason() -> PoisonedHaltReason:
    return PoisonedHaltReason(
        trigger=PoisonedHaltTrigger.COLD_START_DIVERGENCE,
        halted_at_ms=1_700_000_000_000,
        last_clean_bar_close_ms=1_699_999_940_000,
        details={"reason": "foreign_perm_id", "source": "reconciliation_orchestrator"},
    )


def test_safety_halt_incident_carries_forensic_payload(tmp_path: Path) -> None:
    artifact_path = tmp_path / "run-1" / "poisoned.flag"
    log_path = tmp_path / "run-1" / "live.log"
    incident = build_safety_halt_incident(
        strategy_instance_id="bot-1",
        run_id="run-1",
        halt_reason=_reason(),
        artifact_path=artifact_path,
        log_path=log_path,
    )

    assert incident.category == "safety-halt"
    assert incident.notice.code == "safety_halt.poisoned"
    assert incident.notice.tier == "critical"
    assert incident.notice.action.kind == "redeploy"
    assert incident.started_at_ms == 1_700_000_000_000
    assert incident.evidence["strategy_instance_id"] == "bot-1"
    assert incident.evidence["run_id"] == "run-1"
    assert incident.evidence["halt_trigger"] == "cold_start_divergence"
    assert incident.evidence["source_flag"] == "poisoned.flag"
    assert incident.evidence["artifact_path"] == str(artifact_path)
    assert incident.evidence["log_path"] == str(log_path)


def test_safety_halt_incident_id_is_deterministic() -> None:
    reason = _reason()

    first = safety_halt_incident_id(
        strategy_instance_id="bot-1",
        run_id="run-1",
        halt_reason=reason,
        artifact_path=Path("/tmp/run-1/poisoned.flag"),
    )
    second = safety_halt_incident_id(
        strategy_instance_id="bot-1",
        run_id="run-1",
        halt_reason=reason,
        artifact_path=Path("/tmp/run-1/poisoned.flag"),
    )

    assert first == second
    assert first.startswith("safety-halt-")
