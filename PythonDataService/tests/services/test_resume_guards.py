"""PRD #616 — three-layer unit tests for the shared Resume guard.

Layer 1: pure folds for each artifact reader (verdict snapshot,
reconciliation receipt, WAL uncertain-intent scan).

Layer 2: composed ``ResumeGuardState`` resolver over each
artifact-state combination from the shared ``GUARD_CASES`` table.

Tests exercise the resolver as a black box: artifact selection and
freshness validation live inside the resolver, and consumers never
poke at the resolver's internal state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.resume_guard_state import (
    RESUME_REASON_CODES,
    BrokerSafetyArtifact,
    ReconciliationArtifact,
    UncertainIntentArtifact,
    empty_guard_state,
    read_broker_safety_verdict,
    read_reconciliation_receipt,
    read_uncertain_intent_state,
    resolve_guard_state,
    resolve_guard_state_from_paths,
    sort_reason_codes,
)
from tests._fixtures.resume_guard_cases import GUARD_CASES, GuardCase

# ---------------------------------------------------------------------------
# Layer 1 — pure folds
# ---------------------------------------------------------------------------


def test_read_broker_safety_verdict_missing_file_is_unknown(tmp_path: Path) -> None:
    artifact = read_broker_safety_verdict(tmp_path / "verdict_snapshot.json")
    assert artifact.state == "UNKNOWN"
    assert artifact.verdict is None


@pytest.mark.parametrize(
    ("verdict_value", "expected_state"),
    [
        ("paper-only", "SAFE"),
        ("unsafe", "UNSAFE"),
        ("unknown", "UNKNOWN"),
        ("garbled", "UNKNOWN"),
    ],
)
def test_read_broker_safety_verdict_maps_verdict_string(
    tmp_path: Path, verdict_value: str, expected_state: str
) -> None:
    path = tmp_path / "verdict_snapshot.json"
    path.write_text(json.dumps({"verdict": verdict_value}), encoding="utf-8")
    artifact = read_broker_safety_verdict(path)
    assert artifact.state == expected_state


def test_read_broker_safety_verdict_corrupt_file_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "verdict_snapshot.json"
    path.write_text("{not json}", encoding="utf-8")
    artifact = read_broker_safety_verdict(path)
    assert artifact.state == "UNKNOWN"


def test_read_reconciliation_receipt_missing_is_not_available(tmp_path: Path) -> None:
    artifact = read_reconciliation_receipt(tmp_path)
    assert artifact.state == "NOT_AVAILABLE"


def test_read_reconciliation_receipt_passed(tmp_path: Path) -> None:
    receipt = tmp_path / "reconciliation_receipt.json"
    receipt.write_text(
        json.dumps({"status": "passed", "last_reconcile_ms": 1_700_000_000_000}),
        encoding="utf-8",
    )
    artifact = read_reconciliation_receipt(tmp_path)
    assert artifact.state == "PASSED"


def test_read_reconciliation_receipt_failed_carries_detail(tmp_path: Path) -> None:
    receipt = tmp_path / "reconciliation_receipt.json"
    receipt.write_text(
        json.dumps({"status": "failed", "detail": "residual SPY +1"}),
        encoding="utf-8",
    )
    artifact = read_reconciliation_receipt(tmp_path)
    assert artifact.state == "FAILED"
    assert artifact.detail == "residual SPY +1"


def test_read_reconciliation_receipt_stale_when_receipt_predates(tmp_path: Path) -> None:
    receipt = tmp_path / "reconciliation_receipt.json"
    receipt.write_text(
        json.dumps({"status": "passed", "last_reconcile_ms": 100}),
        encoding="utf-8",
    )
    artifact = read_reconciliation_receipt(tmp_path, relevant_after_ms=200)
    assert artifact.state == "STALE"


def test_read_reconciliation_receipt_unreadable_is_unknown(tmp_path: Path) -> None:
    receipt = tmp_path / "reconciliation_receipt.json"
    receipt.write_text("garbage", encoding="utf-8")
    artifact = read_reconciliation_receipt(tmp_path)
    assert artifact.state == "UNKNOWN"


def test_read_uncertain_intent_state_missing_wal_is_clear(tmp_path: Path) -> None:
    artifact = read_uncertain_intent_state(tmp_path / "intent_events.jsonl")
    assert artifact.state == "CLEAR"


def test_read_uncertain_intent_state_present_when_uncertain_event(tmp_path: Path) -> None:
    wal = tmp_path / "intent_events.jsonl"
    from app.engine.live.intent_events import IntentEventType
    from app.engine.live.intent_wal import IntentWal

    wal_writer = IntentWal(wal)
    wal_writer.append(
        event_type=IntentEventType.ACK_FAILED_UNCERTAIN,
        intent_id="intent-a",
        bot_order_namespace="ns-1",
        order_ref="ns-1:intent-a",
        ts_ms=1_700_000_000_000,
        reason="ibkr 322",
    )
    artifact = read_uncertain_intent_state(wal)
    assert artifact.state == "PRESENT"
    assert "intent-a" in artifact.unresolved_intent_ids


def test_read_uncertain_intent_state_clear_when_resolution_follows(tmp_path: Path) -> None:
    wal = tmp_path / "intent_events.jsonl"
    from app.engine.live.intent_events import IntentEventType
    from app.engine.live.intent_wal import IntentWal

    wal_writer = IntentWal(wal)
    wal_writer.append(
        event_type=IntentEventType.ACK_FAILED_UNCERTAIN,
        intent_id="intent-a",
        bot_order_namespace="ns-1",
        order_ref="ns-1:intent-a",
        ts_ms=1_700_000_000_000,
    )
    wal_writer.append(
        event_type=IntentEventType.SUBMITTED_RECOVERED,
        intent_id="intent-a",
        bot_order_namespace="ns-1",
        order_ref="ns-1:intent-a",
        ts_ms=1_700_000_000_100,
    )
    artifact = read_uncertain_intent_state(wal)
    assert artifact.state == "CLEAR"


def test_read_uncertain_intent_state_unknown_when_wal_corrupt(tmp_path: Path) -> None:
    wal = tmp_path / "intent_events.jsonl"
    wal.write_text("not-json-at-all\n", encoding="utf-8")
    artifact = read_uncertain_intent_state(wal)
    assert artifact.state == "UNKNOWN"


# ---------------------------------------------------------------------------
# Layer 2 — composed resolver, every guard combination via the shared table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", GUARD_CASES, ids=lambda c: c.name)
def test_resolve_guard_state_matches_table(case: GuardCase) -> None:
    state = resolve_guard_state(
        broker_safety=case.broker_safety,
        submission_capability=case.submission_capability,
        reconciliation=case.reconciliation,
        uncertain_intent=case.uncertain_intent,
    )
    assert state.allow_resume is case.expected_allow_resume
    assert tuple(state.reason_codes) == case.expected_reason_codes


def test_resolve_guard_state_from_paths_composes_artifact_readers(tmp_path: Path) -> None:
    # All three artifacts in their happy state.
    (tmp_path / "verdict_snapshot.json").write_text(
        json.dumps({"verdict": "paper-only"}), encoding="utf-8"
    )
    # PRD #619-A — run_status.json carries the durable child/run
    # capability evidence (declared submit_mode + actual readonly at
    # child construction).
    (tmp_path / "run_status.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": "r",
                "started_at_ms": 1,
                "last_update_ms": 1,
                "host_pid": 1,
                "submit_mode_at_start": "live_paper",
                "readonly_at_start": False,
            }
        ),
        encoding="utf-8",
    )
    state = resolve_guard_state_from_paths(
        verdict_snapshot_path=tmp_path / "verdict_snapshot.json",
        run_status_path=tmp_path / "run_status.json",
        run_dir_for_reconciliation=tmp_path,
        intent_wal_path=tmp_path / "intent_events.jsonl",
    )
    assert state.allow_resume is True
    assert state.broker_safety.state == "SAFE"
    assert state.submission_capability.state == "SATISFIED"
    assert state.reconciliation.state == "NOT_AVAILABLE"
    assert state.uncertain_intent.state == "CLEAR"


def test_empty_guard_state_permits_resume() -> None:
    state = empty_guard_state()
    assert state.allow_resume is True
    assert state.reason_codes == []


def test_sort_reason_codes_preserves_priority_for_unknown_codes_last() -> None:
    sorted_codes = sort_reason_codes(["RECONCILIATION_FAILED", "BROKER_SAFETY_UNSAFE", "WAT_UNKNOWN_CODE"])
    # Documented codes come first in priority order, unknowns trail.
    assert sorted_codes == [
        "BROKER_SAFETY_UNSAFE",
        "RECONCILIATION_FAILED",
        "WAT_UNKNOWN_CODE",
    ]


def test_resume_reason_codes_vocabulary_pinned() -> None:
    # PRD #616 / PRD #619-A — closed vocabulary the Frontend lookup covers.
    assert (
        frozenset(
            {
                "BROKER_SAFETY_UNSAFE",
                "BROKER_SAFETY_UNKNOWN",
                "SUBMISSION_CAPABILITY_BLOCKED",
                "SUBMISSION_CAPABILITY_UNKNOWN",
                "RECONCILIATION_FAILED",
                "RECONCILIATION_STALE",
                "RECONCILIATION_NOT_AVAILABLE",
                "RECONCILIATION_UNKNOWN",
                "UNRESOLVED_UNCERTAIN_INTENT",
                "UNCERTAIN_INTENT_STATE_UNKNOWN",
                "DESIRED_STATE_ALREADY_RUNNING",
                "DESIRED_STATE_DEFAULT_RUNNING",
                "ALREADY_PAUSED",
                "STOPPED_REQUIRES_REDEPLOY",
                "REDEPLOY_REQUIRED",
            }
        )
        == RESUME_REASON_CODES
    )


def test_resume_guard_state_carries_artifact_diagnostics() -> None:
    from app.services.resume_guard_state import SubmissionCapabilityArtifact

    state = resolve_guard_state(
        broker_safety=BrokerSafetyArtifact(state="UNSAFE", verdict="unsafe"),
        submission_capability=SubmissionCapabilityArtifact(
            state="SATISFIED",
            declared_submit_mode="live_paper",
            readonly_at_start=False,
        ),
        reconciliation=ReconciliationArtifact(state="FAILED", detail="residual SPY +1", receipt_path="/x"),
        uncertain_intent=UncertainIntentArtifact(state="PRESENT", unresolved_intent_ids=("intent-x",)),
    )
    # Diagnostics remain available to the CLI without re-reading artifacts.
    assert state.broker_safety.verdict == "unsafe"
    assert state.reconciliation.detail == "residual SPY +1"
    assert state.uncertain_intent.unresolved_intent_ids == ("intent-x",)
