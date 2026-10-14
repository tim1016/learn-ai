"""A pre-v12 ``ACCOUNT_HOLD_RESOLVED`` row still replays into its uncertainty episode.

``ACCOUNT_HOLD_RESOLVED`` is retired as a write kind (ADR 0048 Decision 2) but a
mirror recorded before v12 holds it. ``test_v12_holds_to_uncertainties.py``
replays the raise and the refresh; this is the resolution, built through the
fold registry the way a stored row replays.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.broker.alpaca.clerk.sqlite.facts import AccountHoldRaisedFacts, AccountHoldResolvedFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import UNEXPLAINED_ORDER_HOLD_REASON_CODE

_ACCOUNT_ID = "PA-V12-RESOLVED"


def _clock(start: int = 1_700_000_000_000):
    value = [start]

    def tick() -> int:
        value[0] += 1
        return value[0]

    return tick


def _legacy_transition(kind: str, facts_json: str) -> TransitionInput:
    return TransitionInput(
        transition_kind=kind,
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="succeeded",
        clerk_observed_at_ms=1,
        summary_code=kind,
        facts_json=facts_json,
    )


def _raised(evidence_refs: list[str]) -> TransitionInput:
    facts = AccountHoldRaisedFacts(reason_code="UNEXPLAINED_ORDER", evidence_refs=evidence_refs)
    return _legacy_transition("ACCOUNT_HOLD_RAISED", facts.to_facts_json())


def _resolved(evidence_refs: list[str]) -> TransitionInput:
    facts = AccountHoldResolvedFacts(reason_code="UNEXPLAINED_ORDER", evidence_refs=evidence_refs)
    return _legacy_transition("ACCOUNT_HOLD_RESOLVED", facts.to_facts_json())


def _holds(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM holds ORDER BY hold_id")]


def test_a_replayed_resolution_closes_the_episode_at_its_own_instant_with_its_own_evidence(
    tmp_path: Path,
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id=_ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock()
    )
    try:
        repo.append_transition(_raised(["bo-a", "bo-b"]))
        raised = _holds(repo._conn)
        assert [hold["state"] for hold in raised] == ["ACTIVE"]

        repo.append_transition(_resolved(["bo-a", "bo-b", "reviewed"]))
        closing = repo.custody_transitions()[-1]
        resolved = _holds(repo._conn)
        still_active = repo.active_hold(
            scope="ACCOUNT_CLERK", reason_code=UNEXPLAINED_ORDER_HOLD_REASON_CODE
        )
    finally:
        repo.close()

    assert closing["transition_kind"] == "ACCOUNT_HOLD_RESOLVED"
    assert len(resolved) == 1
    row = resolved[0]
    assert row["hold_id"] == raised[0]["hold_id"]
    assert row["reason_code"] == UNEXPLAINED_ORDER_HOLD_REASON_CODE
    assert row["state"] == "RESOLVED"
    assert row["opened_at_ms"] == raised[0]["opened_at_ms"]
    assert row["resolved_at_ms"] == closing["recorded_at_ms"]
    assert json.loads(row["evidence_refs_json"]) == ["bo-a", "bo-b", "reviewed"]
    assert still_active is None


def test_a_pre_v12_mirror_ending_in_a_resolution_rebuilds_to_the_same_closed_episode(
    tmp_path: Path,
) -> None:
    clock = _clock()
    repo = ClerkSqliteRepository.initialize(
        account_id=_ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    repo.append_transition(_raised(["bo-a"]))
    repo.append_transition(_resolved(["bo-a"]))
    original_transitions = repo.custody_transitions()
    original_holds = _holds(repo._conn)
    db_path = repo.db_path
    repo.close()

    db_path.rename(db_path.with_suffix(".db.corrupt"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=_ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    try:
        assert rebuilt.custody_transitions() == original_transitions
        replayed = _holds(rebuilt._conn)
    finally:
        rebuilt.close()

    assert original_holds[0]["state"] == "RESOLVED"
    assert replayed == original_holds


def test_a_resolution_with_no_open_episode_changes_nothing_and_a_second_never_reopens_the_first(
    tmp_path: Path,
) -> None:
    """The fold closes only an open episode: a stray or repeated resolution is inert."""
    repo = ClerkSqliteRepository.initialize(
        account_id=_ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock()
    )
    try:
        repo.append_transition(_resolved(["orphan"]))
        assert _holds(repo._conn) == []

        repo.append_transition(_raised(["bo-a"]))
        repo.append_transition(_resolved(["bo-a"]))
        closed = _holds(repo._conn)
        repo.append_transition(_resolved(["bo-a", "late"]))
        after_second = _holds(repo._conn)
    finally:
        repo.close()

    assert closed[0]["state"] == "RESOLVED"
    assert after_second == closed
