"""Opening, migrating and reopening the profiles database.

"Done when: create/edit/reload/stage/apply survives a service restart …
migrations are repeatable and preserve rows" (plan §6, package B). A restart is
exactly close-and-reopen from the same path, which is what these assert; that
the path survives ``podman compose down -v`` is a property of the external
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

    The shipped registry is empty because v1 is the initial schema, so the
    first *real* upgrade must be a table entry rather than a mechanism designed
    under pressure. This proves the mechanism now.
    """
    first = _service_on(clerk_dir, clock)
    created = paper_profile(first)
    first.close()

    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(
        schema,
        "SCHEMA_MIGRATIONS",
        {1: ("ALTER TABLE broker_profiles ADD COLUMN migration_probe TEXT",)},
    )

    upgraded = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        assert upgraded.schema_version == 2
        assert upgraded.read_profile(created.profile.profile_id) is not None
    finally:
        upgraded.close()

    # Replaying the same open is a no-op, not a second ALTER.
    replayed = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        assert replayed.schema_version == 2
        assert replayed.read_profile(created.profile.profile_id) is not None
    finally:
        replayed.close()


def test_a_partially_failing_migration_leaves_the_version_untouched(
    clerk_dir: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _service_on(clerk_dir, clock)
    created = paper_profile(first)
    first.close()

    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(
        schema,
        "SCHEMA_MIGRATIONS",
        {
            1: (
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
