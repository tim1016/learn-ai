"""The registered registry migrations (audit 2026-09-13; #2073b).

A v1 registry — the only shape the pre-hardening spine ever wrote, and one
that exists in no production deployment — must upgrade to v2 inside one
transaction, preserving every clerk, session, assignment and receipt row,
mapping the old ``failed`` receipt outcome to ``provider_refused``, and
installing the confirmed/namespace/endpoint fences the hardened protocol
expects.

The v2 → v3 upgrade, which does run against live registries, installs the
nested-volume-root fence. That is a loud failure only for the equal-root
half: the partial ``UNIQUE`` index is validated against existing rows at
``CREATE INDEX`` time, so a registry already carrying two equal roots
refuses the upgrade. The ``BEFORE INSERT`` trigger is not — it only guards
rows inserted after the upgrade, so a registry that already carries a
nested (not equal) pair migrates cleanly, silently short one fence.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.broker.fleet import schema
from app.broker.fleet.store import FleetRegistryStore, registry_database_path


def _build_v1_registry(control_dir: Path) -> None:
    """Materialize a populated v1 registry exactly as the old spine wrote it."""
    db_path = registry_database_path(control_dir)
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        schema.configure_connection(conn)
        conn.executescript(schema.SCHEMA_DDL_V1)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO fleet_meta (id, schema_version, registry_id, created_at_ms, "
            "updated_at_ms) VALUES (1, 1, 'fltr_legacy0000000000000000', 1, 1)"
        )
        conn.execute(
            "INSERT INTO clerks (clerk_id, broker, worker_key, display_label, volume_id, "
            "volume_root, volume_attestation_kind, volume_attestation_id, lifecycle_state, "
            "created_at_ms, retired_at_ms) VALUES "
            "('clrk_aaaaaaaaaaaaaaaaaaaaaaaa', 'fake_alpha', 'wkrk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
            "'legacy', 'vol_aaaaaaaaaaaaaaaaaaaaaaaa', '/volumes/legacy', "
            "'compose_named_volume', 'learn-ai-legacy', 'provisioned', 10, NULL)"
        )
        conn.execute(
            "INSERT INTO clerk_sessions (clerk_id, broker, agent_instance_id, routing_epoch, "
            "started_at_ms, last_seen_at_ms, reported_binding_generation, reported_account_id, "
            "reported_state) VALUES ('clrk_aaaaaaaaaaaaaaaaaaaaaaaa', 'fake_alpha', "
            "'agnt_aaaaaaaaaaaaaaaaaaaaaaaa', 4, 20, 30, 2, 'ACCT-LEGACY', 'binding_confirmed')"
        )
        conn.execute(
            "INSERT INTO account_assignments (broker, canonical_external_account_id, clerk_id, "
            "assignment_generation, state, effective_profile_id, effective_revision, "
            "recorded_at_ms, updated_at_ms) VALUES ('fake_alpha', 'ACCT-LEGACY', "
            "'clrk_aaaaaaaaaaaaaaaaaaaaaaaa', 2, 'effective', 'prof_1', 3, 40, 50)"
        )
        conn.execute(
            "INSERT INTO account_assignment_history (broker, canonical_external_account_id, "
            "clerk_id, assignment_generation, state, effective_profile_id, effective_revision, "
            "recorded_at_ms, updated_at_ms) VALUES ('fake_alpha', 'ACCT-LEGACY', "
            "'clrk_aaaaaaaaaaaaaaaaaaaaaaaa', 1, 'released', NULL, NULL, 20, 30)"
        )
        conn.execute(
            "INSERT INTO routing_receipts (correlation_id, broker, clerk_id, operation_kind, "
            "nonsecret_target_ref, idempotency_key, state, upstream_receipt_ref, created_at_ms, "
            "updated_at_ms) VALUES ('corr_aaaaaaaaaaaaaaaaaaaaaaaa', 'fake_alpha', "
            "'clrk_aaaaaaaaaaaaaaaaaaaaaaaa', 'bot_action', 'strategy/sid-1', 'key-1', "
            "'delivered', 'upstream/1', 60, 61)"
        )
        conn.execute(
            "INSERT INTO routing_receipts (correlation_id, broker, clerk_id, operation_kind, "
            "nonsecret_target_ref, idempotency_key, state, upstream_receipt_ref, created_at_ms, "
            "updated_at_ms) VALUES ('corr_bbbbbbbbbbbbbbbbbbbbbbbb', 'fake_alpha', "
            "'clrk_aaaaaaaaaaaaaaaaaaaaaaaa', 'bot_action', 'strategy/sid-2', 'key-2', "
            "'failed', NULL, 62, 63)"
        )
        conn.execute(
            "INSERT INTO routing_receipts (correlation_id, broker, clerk_id, operation_kind, "
            "nonsecret_target_ref, idempotency_key, state, upstream_receipt_ref, created_at_ms, "
            "updated_at_ms) VALUES ('corr_cccccccccccccccccccccccc', 'fake_alpha', "
            "'clrk_aaaaaaaaaaaaaaaaaaaaaaaa', 'bot_action', 'strategy/sid-3', 'key-3', "
            "'outcome_unknown', NULL, 64, 65)"
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def _build_v2_registry(control_dir: Path) -> None:
    """Materialize a populated v2 registry: the v1 shape plus the registered
    v1 → v2 upgrade, so the v2 → v3 case starts from the real v2 DDL rather
    than a reconstruction of it."""
    _build_v1_registry(control_dir)
    conn = sqlite3.connect(registry_database_path(control_dir), isolation_level=None)
    try:
        schema.configure_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        for statement in schema.SCHEMA_MIGRATIONS[1]:
            conn.execute(statement)
        conn.execute("UPDATE fleet_meta SET schema_version = 2 WHERE id = 1")
        conn.execute("COMMIT")
    finally:
        conn.close()


def _build_v3_registry(control_dir: Path) -> None:
    """Materialize a populated v3 registry: the v2 shape plus the registered
    v2 → v3 upgrade, so the v3 → v4 case starts from the real v3 DDL rather
    than a reconstruction of it."""
    _build_v2_registry(control_dir)
    conn = sqlite3.connect(registry_database_path(control_dir), isolation_level=None)
    try:
        schema.configure_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        for statement in schema.SCHEMA_MIGRATIONS[2]:
            conn.execute(statement)
        conn.execute("UPDATE fleet_meta SET schema_version = 3 WHERE id = 1")
        conn.execute("COMMIT")
    finally:
        conn.close()


def _build_v4_registry(control_dir: Path) -> None:
    """Materialize a populated v4 registry: the v3 shape plus the registered
    v3 → v4 upgrade, so the v4 → v5 case starts from the real v4 DDL rather
    than a reconstruction of it."""
    _build_v3_registry(control_dir)
    conn = sqlite3.connect(registry_database_path(control_dir), isolation_level=None)
    try:
        schema.configure_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        for statement in schema.SCHEMA_MIGRATIONS[3]:
            conn.execute(statement)
        conn.execute("UPDATE fleet_meta SET schema_version = 4 WHERE id = 1")
        conn.execute("COMMIT")
    finally:
        conn.close()


def _build_v5_registry(control_dir: Path) -> None:
    """Materialize a populated v5 registry: the v4 shape plus the registered
    v4 → v5 upgrade, so the v5 → v6 case starts from the real v5 DDL rather
    than a reconstruction of it."""
    _build_v4_registry(control_dir)
    conn = sqlite3.connect(registry_database_path(control_dir), isolation_level=None)
    try:
        schema.configure_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        for statement in schema.SCHEMA_MIGRATIONS[4]:
            conn.execute(statement)
        conn.execute("UPDATE fleet_meta SET schema_version = 5 WHERE id = 1")
        conn.execute("COMMIT")
    finally:
        conn.close()


_INSERT_CLERK_SQL = (
    "INSERT INTO clerks (clerk_id, broker, worker_key, display_label, volume_id, "
    "volume_root, deployment_namespace, volume_attestation_kind, "
    "volume_attestation_id, lifecycle_state, created_at_ms, retired_at_ms) "
    "VALUES (?, 'fake_alpha', ?, ?, ?, ?, 'host:local', 'compose_named_volume', "
    "?, 'provisioned', 100, NULL)"
)


def _clerk_values(suffix: str, volume_root: str) -> tuple[str, ...]:
    """One insertable clerk row differing from its siblings only in its root."""
    return (
        f"clrk_{suffix * 24}",
        f"wkrk_{suffix * 32}",
        f"clerk-{suffix}",
        f"vol_{suffix * 24}",
        volume_root,
        f"attest-{suffix}",
    )


def test_a_v1_registry_migrates_preserving_every_row(control_dir: Path) -> None:
    """The registered migration upgrades v1 to v2 with no row lost or weakened."""
    _build_v1_registry(control_dir)
    store = FleetRegistryStore.open(control_dir=control_dir)
    try:
        assert store.schema_version == schema.SCHEMA_VERSION

        clerk = store.read_clerk("clrk_aaaaaaaaaaaaaaaaaaaaaaaa")
        assert clerk is not None
        assert clerk.deployment_namespace == "host:local"

        session = store.read_session("clrk_aaaaaaaaaaaaaaaaaaaaaaaa")
        assert session is not None
        assert session.routing_epoch == 4
        assert session.reported_binding_generation == 2
        assert session.endpoint_ref is None

        assignment = store.read_assignment(
            broker="fake_alpha", canonical_account_id="ACCT-LEGACY"
        )
        assert assignment is not None
        assert assignment.state.value == "effective"
        # The confirmed observation starts empty: pre-v2 evidence was
        # observed, not confirmed, and the hardened protocol re-confirms.
        assert assignment.confirmed_binding_generation is None

        receipts = {
            receipt.idempotency_key: receipt
            for receipt in store.list_routing_receipts()
        }
        assert set(receipts) == {"key-1", "key-2", "key-3"}
        assert receipts["key-1"].state.value == "delivered"
        assert receipts["key-1"].upstream_receipt_ref == "upstream/1"
        # 'failed' maps to the definitive provider refusal; pre-v2 receipts
        # count as dispatched at their last update.
        assert receipts["key-2"].state.value == "provider_refused"
        assert receipts["key-2"].dispatched_at_ms == 63
        assert receipts["key-3"].state.value == "outcome_unknown"
        for receipt in receipts.values():
            assert receipt.pinned_routing_epoch is None

        # The v2 fences are live on the migrated registry.
        with pytest.raises(sqlite3.IntegrityError, match="pinned context"), store.transaction() as conn:
            conn.execute(
                "UPDATE routing_receipts SET pinned_binding_generation = 9 "
                "WHERE correlation_id = 'corr_aaaaaaaaaaaaaaaaaaaaaaaa'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="never downgraded"), store.transaction() as conn:
            conn.execute(
                "UPDATE routing_receipts SET state = 'outcome_unknown' "
                "WHERE correlation_id = 'corr_aaaaaaaaaaaaaaaaaaaaaaaa'"
            )
    finally:
        store.close()


def _snapshot_all_rows(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    """Every row of every table, for a before/after idempotence comparison."""
    tables = [
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    return {
        table: sorted(tuple(row) for row in conn.execute(f"SELECT * FROM {table}"))
        for table in tables
    }


def test_the_migration_is_idempotent_and_reopening_changes_nothing(
    control_dir: Path,
) -> None:
    """Reopening a migrated registry neither re-migrates nor disturbs a row."""
    _build_v1_registry(control_dir)
    first = FleetRegistryStore.open(control_dir=control_dir)
    registry_id = first.registry_id
    before = _snapshot_all_rows(first._conn)
    first.close()
    second = FleetRegistryStore.open(control_dir=control_dir)
    try:
        assert second.schema_version == schema.SCHEMA_VERSION
        assert second.registry_id == registry_id
        assert _snapshot_all_rows(second._conn) == before
    finally:
        second.close()


def test_an_amputated_v1_registry_refuses_rather_than_producing_a_broken_v2(
    tmp_path: Path,
) -> None:
    """A schema version with no registered upgrade path refuses, never guesses."""
    db_path = registry_database_path(tmp_path)
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        schema.configure_connection(conn)
        conn.executescript(schema.SCHEMA_DDL_V1)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO fleet_meta (id, schema_version, registry_id, created_at_ms, "
            "updated_at_ms) VALUES (1, 1, 'fltr_legacy0000000000000000', 1, 1)"
        )
        conn.execute("COMMIT")
        # Amputate one of the v1 tables: the registered migration assumes the
        # full v1 shape, and a registry that is not really v1 must refuse
        # rather than produce a broken v2.
        conn.execute("DROP TABLE routing_receipts")
    finally:
        conn.close()
    from app.broker.fleet.errors import FleetRegistryUnavailable

    try:
        FleetRegistryStore.open(control_dir=tmp_path)
    except FleetRegistryUnavailable:
        return
    except sqlite3.Error:
        return  # a hard SQL refusal is equally fail-closed
    raise AssertionError("an amputated v1 registry must refuse to open")


def test_a_migrated_registry_carries_the_fresh_v2_fences(control_dir: Path, tmp_path: Path) -> None:
    """Regression (independent review): the migration converges on the v2
    schema — the no-delete trigger exists, delivered is terminal, and the
    attestation uniqueness is namespace-scoped, exactly as in a fresh build."""
    _build_v1_registry(control_dir)
    store = FleetRegistryStore.open(control_dir=control_dir)
    try:
        # Object parity: every trigger and index a fresh v2 registry has,
        # the migrated one has too (column-level CHECK parity on an ALTERed
        # table is not expressible in SQLite and stays service-guarded).
        fresh_dir = tmp_path / "fresh"
        fresh = FleetRegistryStore.open(control_dir=fresh_dir)
        try:
            def objects(conn: sqlite3.Connection) -> set[tuple[str, str]]:
                return {
                    (str(row[0]), str(row[1]))
                    for row in conn.execute(
                        "SELECT type, name FROM sqlite_master "
                        "WHERE type IN ('trigger', 'index') AND name NOT LIKE 'sqlite_%'"
                    )
                }

            assert objects(store._conn) == objects(fresh._conn)
        finally:
            fresh.close()

        # The no-delete fence is live on the migrated registry.
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"), store.transaction() as conn:
            conn.execute("DELETE FROM routing_receipts")

        # Attestation uniqueness is namespace-scoped: the same attestation in
        # two namespaces is two volumes, not a collision.
        with store.transaction() as conn:
            for suffix, namespace in (("d", "compose:prod"), ("e", "compose:staging")):
                conn.execute(
                    "INSERT INTO clerks (clerk_id, broker, worker_key, display_label, "
                    "volume_id, volume_root, deployment_namespace, "
                    "volume_attestation_kind, volume_attestation_id, lifecycle_state, "
                    "created_at_ms, retired_at_ms) VALUES (?, 'fake_alpha', ?, ?, ?, "
                    "'/volumes/other', ?, 'compose_named_volume', 'shared-name', "
                    "'provisioned', 100, NULL)",
                    (
                        f"clrk_{suffix * 24}",
                        f"wkrk_{suffix * 32}",
                        f"clerk-{suffix}",
                        f"vol_{suffix * 24}",
                        namespace,
                    ),
                )
        # …and within one namespace it still refuses.
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
            conn.execute(
                "INSERT INTO clerks (clerk_id, broker, worker_key, display_label, "
                "volume_id, volume_root, deployment_namespace, "
                "volume_attestation_kind, volume_attestation_id, lifecycle_state, "
                "created_at_ms, retired_at_ms) VALUES ('clrk_ffffffffffffffffffffffff', "
                "'fake_alpha', 'wkrk_ffffffffffffffffffffffffffffffff', 'dupe', "
                "'vol_ffffffffffffffffffffffff', '/volumes/dupe', 'compose:prod', "
                "'compose_named_volume', 'shared-name', 'provisioned', 100, NULL)"
            )
    finally:
        store.close()


def test_a_v2_registry_gains_the_nested_volume_root_fence(
    control_dir: Path, tmp_path: Path
) -> None:
    """#2073b: the v2 → v3 upgrade installs the nested-root fence byte-for-byte
    as a fresh v3 build carries it, and the fence is live on the migrated
    registry — nesting and equality refuse, a mere name prefix does not."""
    _build_v2_registry(control_dir)
    store = FleetRegistryStore.open(control_dir=control_dir)
    try:
        assert store.schema_version == schema.SCHEMA_VERSION

        def fence_sql(conn: sqlite3.Connection) -> dict[str, str]:
            return {
                str(row[0]): str(row[1])
                for row in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE name IN "
                    "('ux_clerks_volume_root', 'trg_clerks_volume_root_not_nested')"
                )
            }

        migrated = fence_sql(store._conn)
        assert set(migrated) == {
            "ux_clerks_volume_root",
            "trg_clerks_volume_root_not_nested",
        }
        # Byte parity with a fresh build: a migrated registry and a new one
        # enforce the fence with the same statement, not merely the same name.
        fresh = FleetRegistryStore.open(control_dir=tmp_path / "fresh")
        try:
            assert fence_sql(fresh._conn) == migrated
        finally:
            fresh.close()

        # The legacy clerk holds '/volumes/legacy' in namespace 'host:local'.
        # A root nested inside it is one physical volume, and refuses…
        with pytest.raises(sqlite3.IntegrityError, match="one physical volume"), store.transaction() as conn:
            conn.execute(_INSERT_CLERK_SQL, _clerk_values("d", "/volumes/legacy/inner"))
        # …as does a root the legacy clerk's own root nests inside…
        with pytest.raises(sqlite3.IntegrityError, match="one physical volume"), store.transaction() as conn:
            conn.execute(_INSERT_CLERK_SQL, _clerk_values("e", "/volumes"))
        # …and the equal-root case, which the partial UNIQUE index owns.
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"), store.transaction() as conn:
            conn.execute(_INSERT_CLERK_SQL, _clerk_values("f", "/volumes/legacy"))

        # But the comparison is exact: '/volumes/legacy-2' merely starts with
        # the legacy root's characters, and is a separate physical volume.
        with store.transaction() as conn:
            conn.execute(_INSERT_CLERK_SQL, _clerk_values("g", "/volumes/legacy-2"))
    finally:
        store.close()


def _query_plan(conn: sqlite3.Connection, sql: str, parameters: tuple) -> list[str]:
    """The ``detail`` column of ``EXPLAIN QUERY PLAN`` for one query."""
    return [
        str(row[3])
        for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", parameters).fetchall()
    ]


def test_a_v3_registry_gains_the_audit_indexes_and_data_survives(
    control_dir: Path, tmp_path: Path
) -> None:
    """#2133 P2-a: the v3 → v4 upgrade installs the audit read surface's two
    newest-first indexes byte-for-byte as a fresh v4 build carries them, every
    pre-existing row survives the upgrade untouched, and both the global and
    clerk-scoped audit queries actually use an index afterward -- not a full
    table scan under the store's shared connection lock.

    Built from a *populated* v3 registry (clerks, a session, an assignment,
    three routing receipts), not a fresh empty one: the deliverable is that a
    live registry's data survives this migration, not merely that a new
    registry gets the indexes.
    """
    _build_v3_registry(control_dir)
    store = FleetRegistryStore.open(control_dir=control_dir)
    try:
        assert store.schema_version == schema.SCHEMA_VERSION

        def index_sql(conn: sqlite3.Connection) -> dict[str, str]:
            return {
                str(row[0]): str(row[1])
                for row in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE name IN "
                    "('ix_routing_receipts_created_at', 'ix_routing_receipts_clerk_created_at')"
                )
            }

        migrated = index_sql(store._conn)
        assert set(migrated) == {
            "ix_routing_receipts_created_at",
            "ix_routing_receipts_clerk_created_at",
        }
        # Byte parity with a fresh build: a migrated registry and a new one
        # carry the same index definitions, not merely the same names.
        fresh = FleetRegistryStore.open(control_dir=tmp_path / "fresh")
        try:
            assert index_sql(fresh._conn) == migrated
        finally:
            fresh.close()

        # Every pre-existing row survives the upgrade untouched.
        clerk = store.read_clerk("clrk_aaaaaaaaaaaaaaaaaaaaaaaa")
        assert clerk is not None
        assert clerk.deployment_namespace == "host:local"
        session = store.read_session("clrk_aaaaaaaaaaaaaaaaaaaaaaaa")
        assert session is not None
        assert session.routing_epoch == 4
        assignment = store.read_assignment(
            broker="fake_alpha", canonical_account_id="ACCT-LEGACY"
        )
        assert assignment is not None
        assert assignment.state.value == "effective"
        receipts = {
            receipt.idempotency_key: receipt for receipt in store.list_routing_receipts()
        }
        assert set(receipts) == {"key-1", "key-2", "key-3"}
        assert receipts["key-1"].state.value == "delivered"
        assert receipts["key-1"].upstream_receipt_ref == "upstream/1"
        assert receipts["key-2"].state.value == "provider_refused"
        assert receipts["key-3"].state.value == "outcome_unknown"

        # The audit surface's two access paths both use an index afterward --
        # not "SCAN routing_receipts" over the whole append-only table.
        columns = FleetRegistryStore._RECEIPT_COLUMNS
        global_plan = _query_plan(
            store._conn,
            f"SELECT {columns} FROM routing_receipts WHERE created_at_ms >= ? "
            "ORDER BY created_at_ms DESC, correlation_id DESC LIMIT ?",
            (0, 100),
        )
        clerk_plan = _query_plan(
            store._conn,
            f"SELECT {columns} FROM routing_receipts WHERE clerk_id = ? AND created_at_ms >= ? "
            "ORDER BY created_at_ms DESC, correlation_id DESC LIMIT ?",
            ("clrk_aaaaaaaaaaaaaaaaaaaaaaaa", 0, 100),
        )
        assert any("USING INDEX ix_routing_receipts_created_at" in line for line in global_plan), (
            global_plan
        )
        assert not any(line.startswith("SCAN routing_receipts") for line in global_plan), (
            global_plan
        )
        assert any(
            "USING INDEX ix_routing_receipts_clerk_created_at" in line for line in clerk_plan
        ), clerk_plan
        assert not any(line.startswith("SCAN routing_receipts") for line in clerk_plan), (
            clerk_plan
        )
    finally:
        store.close()


def test_a_v4_registry_gains_the_drain_ceremony_schema_and_data_survives(
    control_dir: Path, tmp_path: Path
) -> None:
    """ADR 0063 / #2111: the v4 → v5 upgrade installs the drain ceremony's
    columns, indexes and trigger backstops byte-for-byte as a fresh v5 build
    carries them, every pre-existing row survives with its new columns NULL
    (a pre-v5 history row keeps NULL forever — the immutability trigger
    forbids the backfill UPDATE), the fourth lifecycle arm closes the
    ``provisioned -> retired`` bypass for a clerk that has served, and the
    command-quiet predicate's partial index serves the drain ceremonies'
    read instead of scanning the append-only receipts table.

    The populated v4 registry carries the legacy clerk with a live session
    row and an effective assignment — exactly the "has served" population
    the bypass closure must catch.
    """
    _build_v4_registry(control_dir)
    store = FleetRegistryStore.open(control_dir=control_dir)
    try:
        assert store.schema_version == schema.SCHEMA_VERSION

        # Object parity with a fresh build: the recreated lifecycle trigger,
        # the new write-once trigger, the two partial/plain indexes.
        def objects(conn: sqlite3.Connection) -> set[tuple[str, str]]:
            return {
                (str(row[0]), str(row[1]))
                for row in conn.execute(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE type IN ('trigger', 'index') AND name NOT LIKE 'sqlite_%'"
                )
            }

        fresh = FleetRegistryStore.open(control_dir=tmp_path / "fresh")
        try:
            assert objects(store._conn) == objects(fresh._conn)
        finally:
            fresh.close()

        # Every pre-existing row survives, with the new facts reading as
        # "not present" rather than being fabricated.
        clerk = store.read_clerk("clrk_aaaaaaaaaaaaaaaaaaaaaaaa")
        assert clerk is not None
        assert clerk.lifecycle_state.value == "provisioned"
        assert clerk.draining_since_ms is None
        assert clerk.drain_deadline_at_ms is None
        assert clerk.lane_confirmation is None
        assignment = store.read_assignment(
            broker="fake_alpha", canonical_account_id="ACCT-LEGACY"
        )
        assert assignment is not None
        history = store.list_assignment_history(
            broker="fake_alpha", canonical_account_id="ACCT-LEGACY"
        )
        assert history
        for row in history:
            assert row.lane_confirmation is None
            assert row.attested_operator is None
            assert row.attested_at_ms is None

        # The fourth arm is live on the migrated registry: this clerk holds a
        # live session row (and assignment history), so a direct retirement
        # aborts — where the same v4 registry would have accepted it.
        with pytest.raises(
            sqlite3.IntegrityError, match="retires through draining"
        ), store.transaction() as conn:
            conn.execute(
                "UPDATE clerks SET lifecycle_state = 'retired', retired_at_ms = 999 "
                "WHERE clerk_id = 'clrk_aaaaaaaaaaaaaaaaaaaaaaaa'"
            )

        # The write-once trigger is live: a drain's facts never move.
        with store.transaction() as conn:
            conn.execute(
                "UPDATE clerks SET lifecycle_state = 'draining', draining_since_ms = 500, "
                "drain_deadline_at_ms = 900 WHERE clerk_id = 'clrk_aaaaaaaaaaaaaaaaaaaaaaaa'"
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="written once"
        ), store.transaction() as conn:
            conn.execute(
                "UPDATE clerks SET drain_deadline_at_ms = 901 "
                "WHERE clerk_id = 'clrk_aaaaaaaaaaaaaaaaaaaaaaaa'"
            )

        # The command-quiet predicate reads through the partial index.
        columns = FleetRegistryStore._RECEIPT_COLUMNS
        quiet_plan = _query_plan(
            store._conn,
            f"SELECT {columns} FROM routing_receipts WHERE clerk_id = ? "
            "AND state = 'not_dispatched' AND dispatched_at_ms IS NOT NULL",
            ("clrk_aaaaaaaaaaaaaaaaaaaaaaaa",),
        )
        assert any(
            "USING INDEX ix_routing_receipts_unsettled" in line for line in quiet_plan
        ), quiet_plan
        assert not any(line.startswith("SCAN routing_receipts") for line in quiet_plan), (
            quiet_plan
        )
    finally:
        store.close()


def test_a_v5_registry_gains_the_lane_quiet_confirmation_table_and_data_survives(
    control_dir: Path, tmp_path: Path
) -> None:
    """ADR 0063 Decision 2 / #2154: the v5 → v6 upgrade installs the lane-quiet
    confirmation table, its lookup index and both append-only triggers, exactly
    as a fresh v6 build carries them, and every pre-existing row survives.

    The table starts empty on an upgraded registry, which is the correct
    reading: no lane has confirmed, so no lane retires on the normal path
    until one does. An upgrade that fabricated a confirmation would open the
    gate the ceremony exists to keep shut.
    """
    _build_v5_registry(control_dir)
    store = FleetRegistryStore.open(control_dir=control_dir)
    try:
        assert store.schema_version == schema.SCHEMA_VERSION

        def objects(conn: sqlite3.Connection) -> set[tuple[str, str]]:
            return {
                (str(row[0]), str(row[1]))
                for row in conn.execute(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE type IN ('trigger', 'index', 'table') AND name NOT LIKE 'sqlite_%'"
                )
            }

        def lane_quiet_ddl(conn: sqlite3.Connection) -> dict[str, str]:
            return {
                str(row[0]): " ".join(str(row[1]).split())
                for row in conn.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE sql IS NOT NULL AND tbl_name = 'clerk_lane_confirmations'"
                )
            }

        fresh = FleetRegistryStore.open(control_dir=tmp_path / "fresh-v6")
        try:
            assert objects(store._conn) == objects(fresh._conn)
            # Not just the object names: the DDL text itself. This table is
            # written twice — once as the fresh schema, once as the v5 -> v6
            # migration — and nothing else would notice the two drifting
            # apart. An upgraded registry would then quietly carry different
            # constraints from a newly built one.
            migrated_ddl = lane_quiet_ddl(store._conn)
            assert migrated_ddl == lane_quiet_ddl(fresh._conn)
            assert len(migrated_ddl) == 4
            # The gate's ordering key is the registry's own append sequence,
            # never the lane-supplied instant (#2154).
            assert "confirmation_seq INTEGER PRIMARY KEY" in (
                migrated_ddl["clerk_lane_confirmations"]
            )
            assert "confirmation_seq DESC" in (
                migrated_ddl["ix_clerk_lane_confirmations_latest"]
            )
        finally:
            fresh.close()

        clerk = store.read_clerk("clrk_aaaaaaaaaaaaaaaaaaaaaaaa")
        assert clerk is not None
        assert clerk.lifecycle_state.value == "provisioned"
        assert store.read_latest_lane_quiet_confirmation(clerk.clerk_id) is None
    finally:
        store.close()
