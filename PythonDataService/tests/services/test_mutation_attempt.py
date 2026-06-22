"""PRD #619-D1 — tests for the durable mutation_attempt module.

Three concerns are exercised:

1. **Repository atomicity & round-trip** — write + read returns the
   same record; a write that overlaps a stale ``.tmp`` from a previous
   crashed attempt still completes cleanly; the canonical file is
   replaced in one POSIX rename, never observed half-formed.
2. **State-machine legality** — every legal source/target pair is
   accepted; every illegal pair raises ``InvalidMutationTransitionError``
   with the two states attached for the router's structured surfacing.
3. **``latest_for`` semantics** — the most recent (by
   ``requested_at_ms``) attempt for the supplied ``instance_id`` is
   returned; other instances are filtered out; absent storage returns
   ``None`` rather than raising.

The tests are deliberately *not* parameterized over file-system fault
injection — atomicity is inherited from the writer pattern already
exercised by ``test_engine_runtime_writer.py``.  Repeating that here
would test the platform, not the module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.mutation_attempt import (
    _LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    DispatchState,
    InvalidMutationTransitionError,
    MutationAttempt,
    MutationAttemptRepo,
    ReconciliationEvidence,
    ReconciliationOutcome,
    reconcile_mutation_effect,
    transition_attempt,
)


def _attempt(
    *,
    attempt_id: str = "att-1",
    instance_id: str = "inst-A",
    action: str = "start",
    requested_at_ms: int = 1_700_000_000_000,
    state: DispatchState = "PREPARED",
    outcome: dict | None = None,
    evidence: dict | None = None,
) -> MutationAttempt:
    return MutationAttempt(
        mutation_attempt_id=attempt_id,
        instance_id=instance_id,
        run_id=None,
        action=action,  # type: ignore[arg-type]
        requested_at_ms=requested_at_ms,
        last_transition_at_ms=requested_at_ms,
        dispatch_state=state,
        outcome=outcome,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Repository round-trip
# ---------------------------------------------------------------------------


def test_write_then_read_returns_equivalent_record(tmp_path: Path) -> None:
    repo = MutationAttemptRepo(tmp_path)
    attempt = _attempt()

    repo.write(attempt)
    loaded = repo.read("att-1")

    assert loaded == attempt


def test_write_creates_root_directory_if_absent(tmp_path: Path) -> None:
    root = tmp_path / "does" / "not" / "exist"
    repo = MutationAttemptRepo(root)

    repo.write(_attempt())

    assert (root / "att-1.json").exists()


def test_read_missing_attempt_returns_none(tmp_path: Path) -> None:
    repo = MutationAttemptRepo(tmp_path)

    assert repo.read("never-written") is None


def test_read_malformed_artifact_returns_none(tmp_path: Path) -> None:
    repo = MutationAttemptRepo(tmp_path)
    repo.root.mkdir(parents=True, exist_ok=True)
    (repo.root / "att-bad.json").write_text("{not json", encoding="utf-8")

    assert repo.read("att-bad") is None


def test_read_forward_incompatible_schema_returns_none(tmp_path: Path) -> None:
    repo = MutationAttemptRepo(tmp_path)
    repo.root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 99,
        "mutation_attempt_id": "att-future",
        "instance_id": "inst-A",
        "run_id": None,
        "action": "start",
        "requested_at_ms": 1,
        "last_transition_at_ms": 1,
        "dispatch_state": "PREPARED",
        "outcome": None,
        "evidence": None,
    }
    (repo.root / "att-future.json").write_text(json.dumps(payload), encoding="utf-8")

    assert repo.read("att-future") is None


def test_write_overwrites_prior_record_at_same_id(tmp_path: Path) -> None:
    repo = MutationAttemptRepo(tmp_path)
    repo.write(_attempt(state="PREPARED"))
    repo.write(_attempt(state="DISPATCHING", requested_at_ms=1_700_000_001_000))

    loaded = repo.read("att-1")
    assert loaded is not None
    assert loaded.dispatch_state == "DISPATCHING"
    assert loaded.requested_at_ms == 1_700_000_001_000


def test_write_with_leftover_tmp_file_still_replaces_atomically(tmp_path: Path) -> None:
    # Simulate a previous process crashing mid-write: an orphaned
    # ``att-1.json.tmp`` is sitting on disk.  The next write must
    # overwrite the tmp via the new ``open`` and then replace cleanly.
    repo = MutationAttemptRepo(tmp_path)
    repo.root.mkdir(parents=True, exist_ok=True)
    (repo.root / "att-1.json.tmp").write_text("garbage from crashed write", encoding="utf-8")

    repo.write(_attempt())

    loaded = repo.read("att-1")
    assert loaded is not None
    assert loaded.mutation_attempt_id == "att-1"
    # The tmp file is consumed by the replace step.
    assert not (repo.root / "att-1.json.tmp").exists()


# ---------------------------------------------------------------------------
# latest_for semantics
# ---------------------------------------------------------------------------


def test_latest_for_returns_most_recent_by_requested_at_ms(tmp_path: Path) -> None:
    repo = MutationAttemptRepo(tmp_path)
    repo.write(_attempt(attempt_id="att-1", requested_at_ms=1))
    repo.write(_attempt(attempt_id="att-3", requested_at_ms=3))
    repo.write(_attempt(attempt_id="att-2", requested_at_ms=2))

    latest = repo.latest_for("inst-A")
    assert latest is not None
    assert latest.mutation_attempt_id == "att-3"


def test_latest_for_filters_other_instances(tmp_path: Path) -> None:
    repo = MutationAttemptRepo(tmp_path)
    repo.write(_attempt(attempt_id="att-A1", instance_id="inst-A", requested_at_ms=1))
    repo.write(_attempt(attempt_id="att-B1", instance_id="inst-B", requested_at_ms=10))

    latest = repo.latest_for("inst-A")
    assert latest is not None
    assert latest.mutation_attempt_id == "att-A1"
    assert latest.instance_id == "inst-A"


def test_latest_for_returns_none_when_no_attempts(tmp_path: Path) -> None:
    repo = MutationAttemptRepo(tmp_path)
    repo.root.mkdir(parents=True, exist_ok=True)
    assert repo.latest_for("inst-A") is None


def test_latest_for_returns_none_when_root_absent(tmp_path: Path) -> None:
    repo = MutationAttemptRepo(tmp_path / "never-created")
    assert repo.latest_for("inst-A") is None


def test_latest_for_skips_non_json_files(tmp_path: Path) -> None:
    repo = MutationAttemptRepo(tmp_path)
    repo.write(_attempt(attempt_id="att-1", instance_id="inst-A", requested_at_ms=5))
    repo.root.mkdir(parents=True, exist_ok=True)
    (repo.root / "junk.txt").write_text("not an attempt", encoding="utf-8")
    (repo.root / "att-1.json.tmp").write_text("torn write", encoding="utf-8")

    latest = repo.latest_for("inst-A")
    assert latest is not None
    assert latest.mutation_attempt_id == "att-1"


def test_latest_for_skips_malformed_attempts(tmp_path: Path) -> None:
    repo = MutationAttemptRepo(tmp_path)
    repo.write(_attempt(attempt_id="att-good", instance_id="inst-A", requested_at_ms=5))
    (repo.root / "att-corrupt.json").write_text("{not json", encoding="utf-8")

    latest = repo.latest_for("inst-A")
    assert latest is not None
    assert latest.mutation_attempt_id == "att-good"


# ---------------------------------------------------------------------------
# State-machine legality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("PREPARED", "DISPATCHING"),
        ("DISPATCHING", "RESPONSE_CONFIRMED"),
        ("DISPATCHING", "OUTCOME_UNKNOWN"),
        ("RESPONSE_CONFIRMED", "EFFECT_CONFIRMED"),
        ("RESPONSE_CONFIRMED", "EFFECT_NOT_OBSERVED"),
        ("RESPONSE_CONFIRMED", "NOT_PROVABLE"),
        ("RESPONSE_CONFIRMED", "EVIDENCE_CONFLICT"),
        ("OUTCOME_UNKNOWN", "EFFECT_CONFIRMED"),
        ("OUTCOME_UNKNOWN", "EFFECT_NOT_OBSERVED"),
        ("OUTCOME_UNKNOWN", "NOT_PROVABLE"),
        ("OUTCOME_UNKNOWN", "EVIDENCE_CONFLICT"),
    ],
)
def test_legal_transitions_advance_state(source: DispatchState, target: DispatchState) -> None:
    attempt = _attempt(state=source, requested_at_ms=1)

    result = transition_attempt(attempt, target, transitioned_at_ms=2)

    assert result.dispatch_state == target
    assert result.last_transition_at_ms == 2
    assert result.requested_at_ms == 1
    assert result.mutation_attempt_id == "att-1"


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
def test_no_transitions_out_of_terminal_states(terminal: DispatchState) -> None:
    # Every terminal state has an empty legal-successor set; any
    # outgoing transition raises.  Iterate over the full target alphabet
    # to prove there is no accidental escape hatch.
    attempt = _attempt(state=terminal)
    for target in _LEGAL_TRANSITIONS:
        with pytest.raises(InvalidMutationTransitionError) as exc_info:
            transition_attempt(attempt, target, transitioned_at_ms=2)
        assert exc_info.value.current_state == terminal
        assert exc_info.value.requested_state == target


def test_same_state_transition_is_illegal() -> None:
    attempt = _attempt(state="DISPATCHING")

    with pytest.raises(InvalidMutationTransitionError):
        transition_attempt(attempt, "DISPATCHING", transitioned_at_ms=2)


def test_skipping_dispatching_from_prepared_is_illegal() -> None:
    attempt = _attempt(state="PREPARED")

    with pytest.raises(InvalidMutationTransitionError):
        transition_attempt(attempt, "RESPONSE_CONFIRMED", transitioned_at_ms=2)


def test_transition_carries_outcome_when_provided() -> None:
    attempt = _attempt(state="DISPATCHING")

    result = transition_attempt(
        attempt,
        "RESPONSE_CONFIRMED",
        transitioned_at_ms=2,
        outcome={"http_status": 200},
    )

    assert result.outcome == {"http_status": 200}
    assert result.evidence is None


def test_transition_preserves_outcome_when_not_supplied() -> None:
    attempt = _attempt(state="DISPATCHING", outcome={"sent_at_ms": 1})

    result = transition_attempt(attempt, "OUTCOME_UNKNOWN", transitioned_at_ms=2)

    assert result.outcome == {"sent_at_ms": 1}


def test_transition_returns_new_instance_without_mutating_source() -> None:
    attempt = _attempt(state="PREPARED", requested_at_ms=10)

    result = transition_attempt(attempt, "DISPATCHING", transitioned_at_ms=20)

    assert attempt.dispatch_state == "PREPARED"
    assert attempt.last_transition_at_ms == 10
    assert result is not attempt


# ---------------------------------------------------------------------------
# Reconcile (PRD #619-D3) — pure evidence classification.
#
# Each action gets a focused suite covering the four-way outcome.
# ``daemon_reachable=False`` short-circuits to ``NOT_PROVABLE`` for every
# action — exercised once as a cross-action precondition.
# ---------------------------------------------------------------------------


def _evidence(**overrides: object) -> ReconciliationEvidence:
    base: dict[str, object] = {
        "daemon_reachable": True,
        "observed_at_ms": 1_700_000_001_000,
    }
    base.update(overrides)
    return ReconciliationEvidence(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "action",
    ["start", "stop", "resume", "pause", "flatten"],
)
def test_daemon_unreachable_classifies_not_provable_for_every_action(
    action: str,
) -> None:
    attempt = _attempt(action=action, state="OUTCOME_UNKNOWN")
    evidence = _evidence(daemon_reachable=False)

    assert reconcile_mutation_effect(attempt, evidence) == "NOT_PROVABLE"


# --- stop ---


@pytest.mark.parametrize(
    ("process_state", "expected"),
    [
        ("exited", "EFFECT_CONFIRMED"),
        ("idle", "EFFECT_CONFIRMED"),
        ("running", "EFFECT_NOT_OBSERVED"),
        ("stopping", "EFFECT_NOT_OBSERVED"),
        ("unreachable", "NOT_PROVABLE"),
        (None, "NOT_PROVABLE"),
    ],
)
def test_reconcile_stop_classifies_by_process_state(
    process_state: str | None, expected: ReconciliationOutcome
) -> None:
    attempt = _attempt(action="stop", state="OUTCOME_UNKNOWN")
    evidence = _evidence(process_state=process_state)

    assert reconcile_mutation_effect(attempt, evidence) == expected


# --- start ---


def test_reconcile_start_confirmed_when_running_with_binding() -> None:
    attempt = _attempt(action="start", state="OUTCOME_UNKNOWN")
    evidence = _evidence(process_state="running", bound_run_id="run-7")

    assert reconcile_mutation_effect(attempt, evidence) == "EFFECT_CONFIRMED"


def test_reconcile_start_conflict_when_running_without_binding() -> None:
    # Process reports running but daemon has no run binding — the
    # two facts disagree.
    attempt = _attempt(action="start", state="OUTCOME_UNKNOWN")
    evidence = _evidence(process_state="running", bound_run_id=None)

    assert reconcile_mutation_effect(attempt, evidence) == "EVIDENCE_CONFLICT"


@pytest.mark.parametrize("state", ["exited", "idle", "stopping"])
def test_reconcile_start_not_observed_when_not_running(state: str) -> None:
    attempt = _attempt(action="start", state="OUTCOME_UNKNOWN")
    evidence = _evidence(process_state=state)

    assert reconcile_mutation_effect(attempt, evidence) == "EFFECT_NOT_OBSERVED"


@pytest.mark.parametrize("state", ["unreachable", None])
def test_reconcile_start_not_provable_without_process_state(
    state: str | None,
) -> None:
    attempt = _attempt(action="start", state="OUTCOME_UNKNOWN")
    evidence = _evidence(process_state=state)

    assert reconcile_mutation_effect(attempt, evidence) == "NOT_PROVABLE"


# --- resume ---


def test_reconcile_resume_confirmed_full_stack() -> None:
    attempt = _attempt(action="resume", state="OUTCOME_UNKNOWN")
    evidence = _evidence(
        desired_state="RUNNING",
        process_state="running",
        engine_runtime_state="RUNNING",
    )

    assert reconcile_mutation_effect(attempt, evidence) == "EFFECT_CONFIRMED"


def test_reconcile_resume_conflict_when_stopped_supersedes() -> None:
    attempt = _attempt(action="resume", state="OUTCOME_UNKNOWN")
    evidence = _evidence(desired_state="STOPPED")

    assert reconcile_mutation_effect(attempt, evidence) == "EVIDENCE_CONFLICT"


def test_reconcile_resume_not_observed_when_paused_intent_kept() -> None:
    attempt = _attempt(action="resume", state="OUTCOME_UNKNOWN")
    evidence = _evidence(desired_state="PAUSED")

    assert reconcile_mutation_effect(attempt, evidence) == "EFFECT_NOT_OBSERVED"


def test_reconcile_resume_not_observed_when_runtime_not_running() -> None:
    attempt = _attempt(action="resume", state="OUTCOME_UNKNOWN")
    evidence = _evidence(
        desired_state="RUNNING",
        process_state="running",
        engine_runtime_state="PAUSED",
    )

    assert reconcile_mutation_effect(attempt, evidence) == "EFFECT_NOT_OBSERVED"


def test_reconcile_resume_not_observed_when_runtime_missing() -> None:
    attempt = _attempt(action="resume", state="OUTCOME_UNKNOWN")
    evidence = _evidence(
        desired_state="RUNNING",
        process_state="running",
        engine_runtime_state=None,
    )

    assert reconcile_mutation_effect(attempt, evidence) == "EFFECT_NOT_OBSERVED"


def test_reconcile_resume_not_provable_without_desired_state() -> None:
    attempt = _attempt(action="resume", state="OUTCOME_UNKNOWN")
    evidence = _evidence(desired_state=None)

    assert reconcile_mutation_effect(attempt, evidence) == "NOT_PROVABLE"


# --- pause ---


def test_reconcile_pause_confirmed_when_paused() -> None:
    attempt = _attempt(action="pause", state="OUTCOME_UNKNOWN")
    evidence = _evidence(desired_state="PAUSED")

    assert reconcile_mutation_effect(attempt, evidence) == "EFFECT_CONFIRMED"


def test_reconcile_pause_conflict_when_stopped_supersedes() -> None:
    attempt = _attempt(action="pause", state="OUTCOME_UNKNOWN")
    evidence = _evidence(desired_state="STOPPED")

    assert reconcile_mutation_effect(attempt, evidence) == "EVIDENCE_CONFLICT"


def test_reconcile_pause_not_observed_when_running() -> None:
    attempt = _attempt(action="pause", state="OUTCOME_UNKNOWN")
    evidence = _evidence(desired_state="RUNNING")

    assert reconcile_mutation_effect(attempt, evidence) == "EFFECT_NOT_OBSERVED"


def test_reconcile_pause_not_provable_without_desired_state() -> None:
    attempt = _attempt(action="pause", state="OUTCOME_UNKNOWN")
    evidence = _evidence(desired_state=None)

    assert reconcile_mutation_effect(attempt, evidence) == "NOT_PROVABLE"


# --- flatten ---


@pytest.mark.parametrize(
    ("positions_empty", "expected"),
    [
        (True, "EFFECT_CONFIRMED"),
        (False, "EFFECT_NOT_OBSERVED"),
        (None, "NOT_PROVABLE"),
    ],
)
def test_reconcile_flatten_classifies_by_broker_positions(
    positions_empty: bool | None, expected: ReconciliationOutcome
) -> None:
    attempt = _attempt(action="flatten", state="OUTCOME_UNKNOWN")
    evidence = _evidence(broker_owned_positions_empty=positions_empty)

    assert reconcile_mutation_effect(attempt, evidence) == expected


def test_reconcile_is_pure_and_does_not_mutate_attempt() -> None:
    attempt = _attempt(action="stop", state="OUTCOME_UNKNOWN")
    evidence = _evidence(process_state="exited")

    outcome = reconcile_mutation_effect(attempt, evidence)

    assert outcome == "EFFECT_CONFIRMED"
    # The attempt is unchanged; the router is responsible for
    # advancing the dispatch_state via ``transition_attempt``.
    assert attempt.dispatch_state == "OUTCOME_UNKNOWN"


