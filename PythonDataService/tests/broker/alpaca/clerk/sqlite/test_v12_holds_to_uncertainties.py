"""Schema v12: ``holds`` becomes ``uncertainties`` (ADR 0048 Decision 2, #1798).

The migration's whole claim is that a hold was always an uncertainty whose
policy had nowhere to live. These tests hold it to that claim from both ends:
a v11 file carried forward by the backfill, and a pre-v12 mirror carried
forward by replay, must produce the same rows — and every surface that read
``holds`` before must still read it, unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite import hold_migration, reads, schema
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    HOLD_REASON_CODES,
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


def _v11_authority() -> sqlite3.Connection:
    """A real v11 file: the historical v9 schema plus its registered upgrades."""
    conn = sqlite3.connect(":memory:")
    schema.configure_connection(conn)
    conn.row_factory = sqlite3.Row
    schema.apply_v9_schema(conn)
    conn.execute(
        "INSERT INTO control_meta "
        "(id, schema_version, broker, account_id, db_identity_token, authority_generation, "
        "control_revision, created_at_ms, last_open_at_ms, reset_provenance_json, "
        "execution_lease_owner, execution_lease_expires_at_ms) "
        "VALUES (1, 9, 'alpaca', ?, 'identity', 1, 0, 1, 1, NULL, NULL, NULL)",
        (ACCOUNT_ID,),
    )
    for version in (9, 10):
        for statement in schema.SCHEMA_MIGRATIONS[version]:
            conn.execute(statement)
    conn.execute("UPDATE control_meta SET schema_version = 11 WHERE id = 1")
    conn.commit()
    return conn


def _insert_hold(
    conn: sqlite3.Connection,
    *,
    hold_id: str,
    reason_code: str,
    state: str = "ACTIVE",
    opened_at_ms: int = 1_700_000_000_000,
    resolved_at_ms: int | None = None,
    evidence_refs: list[str] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO holds (hold_id, scope, subject_id, strategy_instance_id, reason_code, "
        "state, opened_at_ms, resolved_at_ms, evidence_refs_json) "
        "VALUES (?, 'ACCOUNT_CLERK', NULL, NULL, ?, ?, ?, ?, ?)",
        (
            hold_id,
            reason_code,
            state,
            opened_at_ms,
            resolved_at_ms,
            json.dumps(evidence_refs if evidence_refs is not None else []),
        ),
    )
    conn.commit()


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


def test_an_active_hold_migrates_with_its_identity_and_evidence_intact() -> None:
    conn = _v11_authority()
    _insert_hold(
        conn,
        hold_id="hold:7",
        reason_code="UNEXPLAINED_ORDER",
        opened_at_ms=1_700_000_000_111,
        evidence_refs=["bo-a", "bo-b"],
    )

    schema.migrate_schema(conn, from_version=11)

    row = conn.execute("SELECT * FROM holds WHERE hold_id = 'hold:7'").fetchone()
    assert row is not None
    # The operator-visible identity is the pre-migration one, verbatim.
    assert row["hold_id"] == "hold:7"
    assert row["state"] == "ACTIVE"
    assert row["opened_at_ms"] == 1_700_000_000_111
    assert row["resolved_at_ms"] is None
    assert json.loads(row["evidence_refs_json"]) == ["bo-a", "bo-b"]
    # ...and the stored code is normalised to the spelling the wire already used.
    assert row["reason_code"] == UNEXPLAINED_ORDER_HOLD_REASON_CODE


def test_resolved_holds_migrate_because_they_are_timeline_evidence() -> None:
    """Dropping them would silently rewrite the account's custody history."""
    conn = _v11_authority()
    _insert_hold(
        conn,
        hold_id="hold:3",
        reason_code=STREAM_HEALTH_HOLD_REASON_CODE,
        state="RESOLVED",
        opened_at_ms=1_700_000_000_001,
        resolved_at_ms=1_700_000_000_999,
    )

    schema.migrate_schema(conn, from_version=11)

    row = conn.execute("SELECT * FROM holds WHERE hold_id = 'hold:3'").fetchone()
    assert row is not None
    assert row["state"] == "RESOLVED"
    assert row["resolved_at_ms"] == 1_700_000_000_999


def test_a_resolved_hold_with_no_resolution_stamp_stays_resolved() -> None:
    """The pre-v12 table never constrained the pair; treating such a row as
    active would resurrect a closed account-wide entry fence."""
    conn = _v11_authority()
    _insert_hold(
        conn,
        hold_id="hold:5",
        reason_code=STREAM_HEALTH_HOLD_REASON_CODE,
        state="RESOLVED",
        opened_at_ms=1_700_000_000_042,
        resolved_at_ms=None,
    )

    schema.migrate_schema(conn, from_version=11)

    row = conn.execute("SELECT * FROM holds WHERE hold_id = 'hold:5'").fetchone()
    assert row["state"] == "RESOLVED"
    assert row["resolved_at_ms"] == 1_700_000_000_042


def test_the_backfill_is_deterministic() -> None:
    """Two upgrades of the same v11 input produce byte-identical rows.

    ``uncertainty_id`` is derived from ``hold_id`` rather than minted, so
    nothing in a migrated episode depends on when the upgrade ran.
    """
    def migrated_rows() -> list[tuple]:
        conn = _v11_authority()
        _insert_hold(conn, hold_id="hold:7", reason_code="UNEXPLAINED_ORDER",
                     evidence_refs=["bo-a"])
        _insert_hold(conn, hold_id="hold:8", reason_code=STREAM_HEALTH_HOLD_REASON_CODE,
                     state="RESOLVED", resolved_at_ms=1_700_000_000_500)
        schema.migrate_schema(conn, from_version=11)
        return [
            tuple(row)
            for row in conn.execute("SELECT * FROM uncertainties ORDER BY uncertainty_id")
        ]

    first, second = migrated_rows(), migrated_rows()
    assert first == second
    assert len(first) == 2


def test_an_unregistered_hold_cause_blocks_the_upgrade_instead_of_guessing() -> None:
    """A cause with no policy cannot be carried across truthfully.

    Failing here leaves a readable v11 file naming the fix, which beats an
    authority whose entry fence was reconstructed from an assumption.
    """
    conn = _v11_authority()
    _insert_hold(conn, hold_id="hold:9", reason_code="SOME_FUTURE_CAUSE")

    with pytest.raises(hold_migration.HoldMigrationBlocked, match="SOME_FUTURE_CAUSE"):
        schema.migrate_schema(conn, from_version=11)


def test_a_blocked_upgrade_leaves_the_v11_authority_untouched() -> None:
    conn = _v11_authority()
    _insert_hold(conn, hold_id="hold:9", reason_code="SOME_FUTURE_CAUSE")

    with pytest.raises(hold_migration.HoldMigrationBlocked):
        schema.migrate_schema(conn, from_version=11)

    assert conn.execute(
        "SELECT schema_version FROM control_meta WHERE id = 1"
    ).fetchone()[0] == 11
    assert conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'holds'"
    ).fetchone()[0] == "table"
    assert conn.execute("SELECT COUNT(*) FROM holds").fetchone()[0] == 1


def test_every_migrated_episode_lands_and_none_is_silently_discarded() -> None:
    """A v12 that dropped a blocking episode would remove an account-wide
    entry fence, so the count is asserted rather than assumed."""
    conn = _v11_authority()
    _insert_hold(conn, hold_id="hold:1", reason_code="UNEXPLAINED_ORDER",
                 evidence_refs=["bo-a"])
    _insert_hold(conn, hold_id="hold:2", reason_code=STREAM_HEALTH_HOLD_REASON_CODE,
                 state="RESOLVED", resolved_at_ms=2)

    schema.migrate_schema(conn, from_version=11)

    landed = conn.execute(
        "SELECT COUNT(*) FROM uncertainties WHERE reason_code IN (?, ?)",
        tuple(sorted(HOLD_REASON_CODES)),
    ).fetchone()[0]
    assert landed == 2


def test_migrated_holds_block_new_exposure_and_authorize_no_reduction() -> None:
    """The policy the ADR says a hold always had, now actually declared."""
    conn = _v11_authority()
    _insert_hold(conn, hold_id="hold:7", reason_code="UNEXPLAINED_ORDER",
                 evidence_refs=["bo-a"])

    schema.migrate_schema(conn, from_version=11)

    row = conn.execute(
        "SELECT blocks_new_exposure, allows_reduction FROM uncertainties "
        "WHERE uncertainty_id = 'hold:7'"
    ).fetchone()
    assert row["blocks_new_exposure"] == 1
    assert row["allows_reduction"] == 0


def test_the_two_projections_stay_disjoint_after_the_merge() -> None:
    """Consumers read ``(*holds, *uncertainties)`` as a union.

    A row in both would double-count the moment an account was held, which
    is what the reason-code partition exists to prevent.
    """
    conn = _v11_authority()
    _insert_hold(conn, hold_id="hold:7", reason_code="UNEXPLAINED_ORDER",
                 evidence_refs=["bo-a"])
    schema.migrate_schema(conn, from_version=11)

    hold_ids = {row["hold_id"] for row in conn.execute("SELECT hold_id FROM holds")}
    non_hold_ids = {
        row["uncertainty_id"]
        for row in conn.execute(
            "SELECT uncertainty_id FROM uncertainties WHERE reason_code NOT IN (?, ?)",
            tuple(sorted(HOLD_REASON_CODES)),
        )
    }
    assert hold_ids == {"hold:7"}
    assert hold_ids & non_hold_ids == set()


def test_the_pre_v12_reads_still_answer_against_the_view() -> None:
    """``reads.active_hold`` and ``active_holds_for_admission`` are unchanged
    code; the view is what keeps them working."""
    conn = _v11_authority()
    _insert_hold(conn, hold_id="hold:7", reason_code="UNEXPLAINED_ORDER",
                 evidence_refs=["bo-a"])
    _insert_hold(conn, hold_id="hold:8", reason_code=STREAM_HEALTH_HOLD_REASON_CODE,
                 state="RESOLVED", resolved_at_ms=2)
    schema.migrate_schema(conn, from_version=11)

    active = reads.active_hold(
        conn, scope="ACCOUNT_CLERK", reason_code=UNEXPLAINED_ORDER_HOLD_REASON_CODE
    )
    assert active is not None and active["hold_id"] == "hold:7"
    assert (
        reads.active_hold(
            conn, scope="ACCOUNT_CLERK", reason_code=STREAM_HEALTH_HOLD_REASON_CODE
        )
        is None
    )
    admission = reads.active_holds_for_admission(conn, strategy_instance_id="spy-bot")
    assert [row["hold_id"] for row in admission] == ["hold:7"]


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


def test_replayed_and_migrated_episodes_agree_on_identity(tmp_path: Path) -> None:
    """The two routes into v12 must not disagree about one episode.

    A v11 file carried by the backfill and a pre-v12 mirror carried by replay
    describe the same history. They mint the same ``hold:<sequence>`` id on
    purpose, so an account upgraded one way and rebuilt the other way is the
    same account.
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
        replayed = dict(repo._conn.execute("SELECT * FROM holds").fetchone())
        sequence = repo.custody_transitions()[-1]["sequence"]
    finally:
        repo.close()

    conn = _v11_authority()
    _insert_hold(
        conn,
        hold_id=f"hold:{sequence}",
        reason_code="UNEXPLAINED_ORDER",
        opened_at_ms=replayed["opened_at_ms"],
        evidence_refs=["bo-a"],
    )
    schema.migrate_schema(conn, from_version=11)
    migrated = dict(conn.execute("SELECT * FROM holds").fetchone())

    assert migrated == replayed
