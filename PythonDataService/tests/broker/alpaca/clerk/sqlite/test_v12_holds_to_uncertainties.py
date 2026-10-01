"""Schema v12: ``holds`` becomes ``uncertainties`` (ADR 0048 Decision 2, #1798).

A hold was always an uncertainty whose policy had nowhere to live. These
tests hold the schema to that claim: a pre-v12 mirror carried forward by
replay produces the same rows, and every surface that read ``holds`` before
still reads it, unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite import schema
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    STREAM_HEALTH_HOLD_REASON_CODE,
    UNEXPLAINED_ORDER_HOLD_REASON_CODE,
)

ACCOUNT_ID = "PA-V12"


def _clock(start: int = 1_700_000_000_000):
    value = [start]

    def tick() -> int:
        value[0] += 1
        return value[0]

    return tick


def test_a_fresh_authority_has_no_holds_table_only_the_view() -> None:
    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    schema.apply_schema(conn)

    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'holds'"
    ).fetchone()
    assert row is not None and row[0] == "view"


def test_the_holds_view_refuses_every_write() -> None:
    """A missed write path must fail at its INSERT, not maintain a second copy.

    This is the reason ``holds`` is a view rather than a second table kept in
    step by triggers: a divergent copy of an account-wide entry fence is the
    one outcome worse than a loud failure.
    """
    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    schema.apply_schema(conn)

    with pytest.raises(sqlite3.OperationalError, match="cannot modify holds"):
        conn.execute(
            "INSERT INTO holds (hold_id, scope, reason_code, state, opened_at_ms) "
            "VALUES ('h1', 'ACCOUNT_CLERK', ?, 'ACTIVE', 1)",
            (STREAM_HEALTH_HOLD_REASON_CODE,),
        )


def test_a_live_authority_raises_and_resolves_a_hold_through_the_view(
    tmp_path: Path,
) -> None:
    """End to end on a real repository, not a hand-built connection."""
    from app.broker.alpaca.clerk.sqlite.uncertainty import (
        raise_account_hold,
        resolve_account_hold,
    )

    clock_value = [1_700_000_000_000]

    def clock() -> int:
        clock_value[0] += 1
        return clock_value[0]

    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    try:
        assert (
            raise_account_hold(
                repo,
                reason_code=STREAM_HEALTH_HOLD_REASON_CODE,
                evidence_refs=["market_data: disconnected"],
            )
            == "raised"
        )
        held = repo.active_hold(
            scope="ACCOUNT_CLERK", reason_code=STREAM_HEALTH_HOLD_REASON_CODE
        )
        assert held is not None and held["state"] == "ACTIVE"

        # An unchanged envelope appends nothing: the append-on-change-only gate
        # the stream-health sync depends on, inherited from the uncertainty path.
        assert (
            raise_account_hold(
                repo,
                reason_code=STREAM_HEALTH_HOLD_REASON_CODE,
                evidence_refs=["market_data: disconnected"],
            )
            == "unchanged"
        )

        assert resolve_account_hold(
            repo,
            reason_code=STREAM_HEALTH_HOLD_REASON_CODE,
            summary_code="ACCOUNT_HOLD_RESOLVED_BY_STREAM_RECOVERY",
        )
        assert (
            repo.active_hold(
                scope="ACCOUNT_CLERK", reason_code=STREAM_HEALTH_HOLD_REASON_CODE
            )
            is None
        )
    finally:
        repo.close()


def test_resolve_refuses_a_reason_code_that_is_not_a_hold_cause(
    tmp_path: Path,
) -> None:
    from app.broker.alpaca.clerk.sqlite.uncertainty import resolve_account_hold

    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=lambda: 1
    )
    try:
        with pytest.raises(ValueError, match="not an account-hold cause"):
            resolve_account_hold(
                repo, reason_code="POSITION_DRIFT", summary_code="X"
            )
    finally:
        repo.close()


def test_a_pre_v12_mirror_still_replays_into_a_v12_authority(tmp_path: Path) -> None:
    """The retired write kinds stay registered as read-only replay folds.

    Deleting them would make every mirror recorded before v12 unreplayable —
    exactly the condition ``MirrorChainBroken`` exists to make loud. This
    appends the legacy transitions, destroys the database, and rebuilds from
    the mirror alone.
    """
    from tests.broker.alpaca.clerk.sqlite.conftest import _hold_transition

    clock = _clock()
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    repo.append_transition(
        _hold_transition(reason_code="UNEXPLAINED_ORDER", evidence_refs=["bo-a"])
    )
    original_transitions = repo.custody_transitions()
    original_holds = [
        dict(row) for row in repo._conn.execute("SELECT * FROM holds ORDER BY hold_id")
    ]
    db_path = repo.db_path
    repo.close()

    db_path.rename(db_path.with_suffix(".db.corrupt"))
    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    try:
        assert rebuilt.custody_transitions() == original_transitions
        replayed = [
            dict(row)
            for row in rebuilt._conn.execute("SELECT * FROM holds ORDER BY hold_id")
        ]
        assert replayed == original_holds
        assert replayed[0]["reason_code"] == UNEXPLAINED_ORDER_HOLD_REASON_CODE
    finally:
        rebuilt.close()


def test_a_replayed_refresh_keeps_the_episode_open_instant(
    tmp_path: Path,
) -> None:
    """A refresh restates the evidence; it does not reopen the episode.

    The pre-v12 refresh fold rewrote ``evidence_refs_json`` and nothing else —
    ``holds.opened_at_ms`` stayed at the raise. A replay fold that advanced it
    to the refresh would move the ``since_ms`` an operator reads off the hold.
    """
    from tests.broker.alpaca.clerk.sqlite.conftest import _hold_transition

    clock = _clock()
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    try:
        repo.append_transition(
            _hold_transition(reason_code="UNEXPLAINED_ORDER", evidence_refs=["bo-a"])
        )
        raised = dict(repo._conn.execute("SELECT * FROM holds").fetchone())
        repo.append_transition(
            _hold_transition(
                transition_kind="ACCOUNT_HOLD_REFRESHED",
                reason_code="UNEXPLAINED_ORDER",
                evidence_refs=["bo-a", "bo-b"],
            )
        )
        replayed = dict(repo._conn.execute("SELECT * FROM holds").fetchone())
    finally:
        repo.close()

    assert json.loads(replayed["evidence_refs_json"]) == ["bo-a", "bo-b"]
    assert replayed["opened_at_ms"] == raised["opened_at_ms"]
