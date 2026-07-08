"""PRD #616 — three-layer unit tests for the shared Resume guard.

Layer 1: pure folds for each artifact reader (verdict snapshot,
reconciliation receipt, WAL uncertain-intent scan).

Layer 2: composed ``ResumeGuardState`` resolver over each
artifact-state combination from the shared ``GUARD_CASES`` table.

Layer 3: capability projection (``evaluate_action``) reading the
composed resolver — including the intent-state-pair and poisoned
overlays that layer above the artifact guards.

Tests exercise the resolver as a black box: artifact selection and
freshness validation live inside the resolver, and consumers never
poke at the resolver's internal state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.live_runs import DesiredStateView, InstanceProcessView, LiveBinding
from app.services.operator_capability import evaluate_action
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


# ---------------------------------------------------------------------------
# Layer 3 — capability projection consumes the resolver
# ---------------------------------------------------------------------------


def _proc() -> InstanceProcessView:
    return InstanceProcessView(state="idle")


def _desired(state: str | None) -> DesiredStateView | None:
    if state is None:
        return None
    return DesiredStateView(state=state, path_status="ok")


@pytest.mark.parametrize("case", GUARD_CASES, ids=lambda c: c.name)
def test_evaluate_action_resume_matches_table(case: GuardCase) -> None:
    guard_state = resolve_guard_state(
        broker_safety=case.broker_safety,
        submission_capability=case.submission_capability,
        reconciliation=case.reconciliation,
        uncertain_intent=case.uncertain_intent,
    )
    capability = evaluate_action(
        "resume",
        process=_proc(),
        live_binding=None,
        poisoned=case.poisoned,
        desired_state=_desired(case.current_intent),
        guard_state=guard_state,
    )
    assert capability.enabled is case.expected_resume_enabled, case.name
    assert tuple(capability.disabled_reasons) == case.expected_resume_codes, case.name


@pytest.mark.parametrize("case", GUARD_CASES, ids=lambda c: c.name)
def test_evaluate_action_pause_matches_table(case: GuardCase) -> None:
    guard_state = resolve_guard_state(
        broker_safety=case.broker_safety,
        submission_capability=case.submission_capability,
        reconciliation=case.reconciliation,
        uncertain_intent=case.uncertain_intent,
    )
    capability = evaluate_action(
        "pause",
        process=_proc(),
        live_binding=None,
        poisoned=case.poisoned,
        desired_state=_desired(case.current_intent),
        guard_state=guard_state,
    )
    assert capability.enabled is case.expected_pause_enabled, case.name
    assert tuple(capability.disabled_reasons) == case.expected_pause_codes, case.name


@pytest.mark.parametrize("case", GUARD_CASES, ids=lambda c: c.name)
def test_evaluate_action_stop_matches_table(case: GuardCase) -> None:
    guard_state = resolve_guard_state(
        broker_safety=case.broker_safety,
        submission_capability=case.submission_capability,
        reconciliation=case.reconciliation,
        uncertain_intent=case.uncertain_intent,
    )
    capability = evaluate_action(
        "stop",
        process=_proc(),
        live_binding=None,
        poisoned=case.poisoned,
        desired_state=_desired(case.current_intent),
        guard_state=guard_state,
    )
    assert capability.enabled is case.expected_stop_enabled, case.name
    assert tuple(capability.disabled_reasons) == case.expected_stop_codes, case.name


def test_evaluate_action_resume_returns_priority_ordered_codes() -> None:
    from app.services.resume_guard_state import SubmissionCapabilityArtifact

    state = resolve_guard_state(
        broker_safety=BrokerSafetyArtifact(state="UNSAFE", verdict="unsafe"),
        submission_capability=SubmissionCapabilityArtifact(
            state="SATISFIED",
            declared_submit_mode="live_paper",
            readonly_at_start=False,
        ),
        reconciliation=ReconciliationArtifact(state="FAILED", detail=""),
        uncertain_intent=UncertainIntentArtifact(state="PRESENT", unresolved_intent_ids=("x",)),
    )
    capability = evaluate_action(
        "resume",
        process=_proc(),
        live_binding=None,
        desired_state=_desired("PAUSED"),
        guard_state=state,
    )
    # The single-line tooltip renders disabled_reason_code; the full
    # list is in disabled_reasons.  Priority order is documented in
    # resume_guard_state._REASON_PRIORITY.
    assert capability.disabled_reason_code == "BROKER_SAFETY_UNSAFE"
    assert capability.disabled_reasons == [
        "BROKER_SAFETY_UNSAFE",
        "UNRESOLVED_UNCERTAIN_INTENT",
        "RECONCILIATION_FAILED",
    ]


def test_reason_codes_vocabulary_is_closed_and_aligned() -> None:
    # Sanity: every code emitted by the shared table is in the closed
    # vocabulary.  Drives the "Frontend lookup is exhaustive and
    # unknown codes fail closed" promise.
    seen: set[str] = set()
    for case in GUARD_CASES:
        seen.update(case.expected_reason_codes)
        seen.update(case.expected_resume_codes)
        seen.update(case.expected_pause_codes)
        seen.update(case.expected_stop_codes)
    from app.services.operator_capability import REASON_CODES

    assert seen <= REASON_CODES, sorted(seen - REASON_CODES)


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


def test_evaluate_action_stop_requires_live_binding_for_actuation_effect() -> None:
    # Stop is durable-only when no live binding; LIVE_ACTUATION when bound + running.
    cap = evaluate_action(
        "stop",
        process=InstanceProcessView(state="idle"),
        live_binding=None,
        desired_state=_desired("PAUSED"),
        guard_state=empty_guard_state(),
    )
    assert cap.enabled is True
    assert cap.effect == "DURABLE_ONLY"

    cap_live = evaluate_action(
        "stop",
        process=InstanceProcessView(state="running"),
        live_binding=LiveBinding(run_id="run-1"),
        desired_state=_desired("PAUSED"),
        guard_state=empty_guard_state(),
    )
    assert cap_live.enabled is True
    assert cap_live.effect == "LIVE_ACTUATION"


def test_evaluate_action_unknown_intent_does_not_block() -> None:
    # Intent of None (sidecar absent / never deployed) is treated as
    # effective-RUNNING so Pause is permitted; resume gets
    # DESIRED_STATE_DEFAULT_RUNNING.
    cap_resume = evaluate_action(
        "resume",
        process=InstanceProcessView(state="idle"),
        live_binding=None,
        desired_state=None,
        guard_state=empty_guard_state(),
    )
    assert cap_resume.enabled is False
    assert cap_resume.disabled_reasons == ["DESIRED_STATE_DEFAULT_RUNNING"]


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
    # Diagnostics fields are preserved so the cockpit's expanded view
    # can render the underlying artifact state without re-querying.
    assert state.broker_safety.verdict == "unsafe"
    assert state.reconciliation.detail == "residual SPY +1"
    assert state.uncertain_intent.unresolved_intent_ids == ("intent-x",)
