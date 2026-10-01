"""Opening, migrating and reopening the profiles database.

Create, edit, reload, stage and apply survive a service restart, and migrations
are repeatable and preserve rows (package B). A restart is exactly
close-and-reopen from the same path, which is what these assert; that the path
survives ``podman compose down -v`` is a property of the external
Clerk volume the database sits on, pinned by ``test_clerk_dir_parity.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.broker_configuration import schema
from app.broker_configuration.errors import ProfilesDatabaseUnavailable
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import ProfilesStore, profiles_database_path
from tests.broker_configuration.conftest import OPERATOR_IDENTITY, FrozenClock, paper_profile


def _service_on(clerk_dir: Path, clock: FrozenClock) -> BrokerConfigurationService:
    return BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        worker_restart=None,
        clock=clock,
    )


def test_a_fresh_database_is_created_at_the_current_schema_version(clerk_dir: Path) -> None:
    store = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        assert store.schema_version == schema.SCHEMA_VERSION
        assert store.read_selection().selection_generation == 0
        assert store.read_owner() is None
    finally:
        store.close()


def test_wal_journal_mode_is_configured(clerk_dir: Path) -> None:
    ProfilesStore.open(clerk_dir=clerk_dir).close()
    connection = sqlite3.connect(profiles_database_path(clerk_dir))
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        connection.close()


def test_reopening_preserves_every_record(clerk_dir: Path, clock: FrozenClock) -> None:
    first = _service_on(clerk_dir, clock)
    created = paper_profile(first)
    first.stage_selection(
        profile_id=created.profile.profile_id, revision=1, expected_selection_generation=0
    )
    first.set_nickname("PA000PAPER", nickname="Testing account")
    owner_id = first.owner().owner_id
    first.close()

    reopened = _service_on(clerk_dir, clock)
    try:
        assert reopened.owner().owner_id == owner_id
        assert [profile.profile_id for profile in reopened.list_profiles()] == [
            created.profile.profile_id
        ]
        assert reopened.read_revision(created.profile.profile_id, 1) == created.latest_revision
        assert reopened.selection().staged_profile_id == created.profile.profile_id
        assert reopened.list_nicknames()[0].nickname == "Testing account"
        assert len(reopened.events(limit=50)) == 3
    finally:
        reopened.close()


def test_opening_an_already_current_database_is_a_no_op(clerk_dir: Path, clock: FrozenClock) -> None:
    """Repeatable: a second open neither re-creates the schema nor loses a row."""
    first = _service_on(clerk_dir, clock)
    created = paper_profile(first)
    first.close()

    for _ in range(3):
        reopened = ProfilesStore.open(clerk_dir=clerk_dir)
        try:
            assert reopened.schema_version == schema.SCHEMA_VERSION
            assert reopened.read_profile(created.profile.profile_id) is not None
        finally:
            reopened.close()


def test_a_registered_migration_advances_the_version_and_preserves_rows(
    clerk_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upgrade machinery, exercised with a registered additive step.

    A probe one version past whatever ships, so the next real upgrade stays a
    table entry rather than a mechanism designed under pressure — and so this
    test keeps exercising the machinery rather than an already-current open
    when a real step is added (#2440's v2 -> v3 is exercised against a real v2
    database in ``test_paper_extended_hours_allowances.py``).
    """
    first = _service_on(clerk_dir, clock)
    created = paper_profile(first)
    first.close()

    probe_version = schema.SCHEMA_VERSION + 1
    monkeypatch.setattr(schema, "SCHEMA_VERSION", probe_version)
    monkeypatch.setattr(
        schema,
        "SCHEMA_MIGRATIONS",
        {probe_version - 1: ("ALTER TABLE broker_profiles ADD COLUMN migration_probe TEXT",)},
    )

    upgraded = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        assert upgraded.schema_version == probe_version
        assert upgraded.read_profile(created.profile.profile_id) is not None
    finally:
        upgraded.close()

    # Replaying the same open is a no-op, not a second ALTER.
    replayed = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        assert replayed.schema_version == probe_version
        assert replayed.read_profile(created.profile.profile_id) is not None
    finally:
        replayed.close()


def test_a_partially_failing_migration_leaves_the_version_untouched(
    clerk_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _service_on(clerk_dir, clock)
    created = paper_profile(first)
    first.close()

    probe_version = schema.SCHEMA_VERSION + 1
    monkeypatch.setattr(schema, "SCHEMA_VERSION", probe_version)
    monkeypatch.setattr(
        schema,
        "SCHEMA_MIGRATIONS",
        {
            probe_version - 1: (
                "ALTER TABLE broker_profiles ADD COLUMN migration_probe TEXT",
                "ALTER TABLE table_that_does_not_exist ADD COLUMN nope TEXT",
            )
        },
    )

    with pytest.raises(ProfilesDatabaseUnavailable):
        ProfilesStore.open(clerk_dir=clerk_dir)

    monkeypatch.undo()
    recovered = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        assert recovered.schema_version == schema.SCHEMA_VERSION
        assert recovered.read_profile(created.profile.profile_id) is not None
    finally:
        recovered.close()


def test_a_newer_schema_version_is_refused_rather_than_downgraded(clerk_dir: Path) -> None:
    ProfilesStore.open(clerk_dir=clerk_dir).close()
    connection = sqlite3.connect(profiles_database_path(clerk_dir))
    try:
        connection.execute("UPDATE configuration_meta SET schema_version = 99 WHERE id = 1")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ProfilesDatabaseUnavailable):
        ProfilesStore.open(clerk_dir=clerk_dir)


def test_an_older_version_with_no_registered_path_fails_closed(clerk_dir: Path) -> None:
    ProfilesStore.open(clerk_dir=clerk_dir).close()
    connection = sqlite3.connect(profiles_database_path(clerk_dir))
    try:
        connection.execute("UPDATE configuration_meta SET schema_version = 0 WHERE id = 1")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ProfilesDatabaseUnavailable):
        ProfilesStore.open(clerk_dir=clerk_dir)


def test_an_unreadable_database_refuses_instead_of_crashing(clerk_dir: Path) -> None:
    path = profiles_database_path(clerk_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not a SQLite database")

    with pytest.raises(ProfilesDatabaseUnavailable):
        ProfilesStore.open(clerk_dir=clerk_dir)


def test_a_revision_is_immutable_apart_from_binding_its_pin_once(
    clerk_dir: Path, clock: FrozenClock
) -> None:
    """This rule lives in the schema, not in a caller's memory."""
    service = _service_on(clerk_dir, clock)
    created = paper_profile(service)
    profile_id = created.profile.profile_id
    service.close()

    store = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        for column, value in (
            ("credential_slot", "'rewritten'"),
            ("endpoint_mode", "'live'"),
            ("content_sha256", "'0' "),
            ("created_at_ms", "1"),
        ):
            with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
                conn.execute(
                    f"UPDATE profile_revisions SET {column} = {value} WHERE profile_id = ?",
                    (profile_id,),
                )
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
            conn.execute("DELETE FROM profile_revisions WHERE profile_id = ?", (profile_id,))

        # The one write a revision does admit.
        with store.transaction() as conn:
            assert store.write_account_pin(
                conn, profile_id=profile_id, revision=1, account_id="PA000PAPER", pinned_at_ms=1
            )
    finally:
        store.close()


def test_the_event_log_is_append_only_in_the_schema(
    clerk_dir: Path, clock: FrozenClock
) -> None:
    service = _service_on(clerk_dir, clock)
    paper_profile(service)
    service.close()

    store = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
            conn.execute("UPDATE configuration_events SET action = 'rewritten'")
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
            conn.execute("DELETE FROM configuration_events")
    finally:
        store.close()


def test_the_selection_generation_cannot_move_backwards(clerk_dir: Path) -> None:
    store = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        with store.transaction() as conn:
            conn.execute("UPDATE installation_selection SET selection_generation = 5 WHERE id = 1")

        with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
            conn.execute("UPDATE installation_selection SET selection_generation = 4 WHERE id = 1")

        assert store.read_selection().selection_generation == 5
    finally:
        store.close()


def test_foreign_keys_are_enforced(clerk_dir: Path) -> None:
    store = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        with pytest.raises(sqlite3.IntegrityError), store.transaction() as conn:
            conn.execute(
                "INSERT INTO broker_profiles (profile_id, owner_id, broker, display_name, "
                "archived, created_at_ms, updated_at_ms) VALUES ('p', 'ghost-owner', 'alpaca', "
                "'Orphan', 0, 0, 0)"
            )
    finally:
        store.close()


def test_v4_upgrade_keeps_old_hashes_and_separates_new_four_field_revisions(clerk_dir: Path, clock: FrozenClock) -> None:
    from app.broker_configuration.envelope import FLOAT_FIELDS, ValidatedLiveEnvelope
    from app.broker_configuration.service import revision_content_sha256
    from tests.broker_configuration.test_paper_extended_hours_allowances import _service, _v2_database

    _v2_database(clerk_dir)
    path = profiles_database_path(clerk_dir)
    with sqlite3.connect(path) as conn:
        for version in (2, 3):
            for statement in schema.SCHEMA_MIGRATIONS[version]:
                conn.execute(statement)
        conn.execute("UPDATE configuration_meta SET schema_version = 4")
        before = conn.execute("SELECT profile_id, revision, content_sha256 FROM profile_revisions ORDER BY profile_id, revision").fetchall()
    service = _service(clerk_dir, clock)
    try:
        profile = next(p for p in service.list_profiles() if p.display_name == "Live — primary")
        legacy = service.read_revision(profile.profile_id, 1)
        assert legacy.live_envelope.shadow_sessions is not None
        assert revision_content_sha256(credential_slot=legacy.credential_slot, endpoint_mode="live", live_envelope=legacy.live_envelope) == legacy.content_sha256
        current = ValidatedLiveEnvelope.from_mapping({name: getattr(legacy.live_envelope, name) for name in FLOAT_FIELDS})
        new = service.create_revision(profile.profile_id, expected_revision=1, credential_slot=legacy.credential_slot, endpoint_mode="live", live_envelope=current)
        assert new.revision == 2
        assert new.content_sha256 != legacy.content_sha256
        assert service.read_revision(profile.profile_id, 1) == legacy
        assert service.read_revision(profile.profile_id, 2).live_envelope.to_mapping() == current.to_mapping()
    finally:
        service.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT profile_id, revision, content_sha256 FROM profile_revisions WHERE revision = 1 ORDER BY profile_id, revision").fetchall() == before
        row = conn.execute("SELECT live_loss_fraction, live_loss_usd, live_shadow_sessions, live_arming_max_sessions, live_xh_entry_bps, live_xh_exit_bps, current_live_envelope_json FROM profile_revisions WHERE revision=2").fetchone()
        assert row[:6] == (None,) * 6
        assert row[6] is not None
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE profile_revisions SET current_live_envelope_json = '{}' WHERE revision = 2")
        # A direct writer cannot install both sources of truth, even on INSERT.
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            columns = [r[1] for r in conn.execute("PRAGMA table_info(profile_revisions)")]
            values = list(conn.execute("SELECT * FROM profile_revisions WHERE revision=2").fetchone())
            values[columns.index("revision")] = 3
            for name, value in (("live_loss_fraction", .05), ("live_loss_usd", 5000.), ("live_shadow_sessions", 1), ("live_arming_max_sessions", 20), ("live_xh_entry_bps", 10.), ("live_xh_exit_bps", 10.)):
                values[columns.index(name)] = value
            conn.execute(f"INSERT INTO profile_revisions VALUES ({','.join('?' for _ in values)})", values)
    reopened = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        assert reopened.read_revision(profile.profile_id, 1) == legacy
        assert reopened.read_revision(profile.profile_id, 2).live_envelope == current
    finally:
        reopened.close()
