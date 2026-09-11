"""Paper clean-slate reset crosses custody and saved configuration together."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.active_runtime import developer_reset_refusal
from app.broker.alpaca.clerk.sqlite.dev_reset import (
    DeveloperCleanSlateResetRefused,
    developer_clean_slate_reset,
)
from app.broker.alpaca.clerk.sqlite.developer_reset_registry import DeveloperCleanSlateResetRegistry
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker_configuration.envelope import ValidatedLiveEnvelope
from app.broker_configuration.errors import (
    ProfilesDatabaseUnavailable,
    RevisionConflict,
    SelectionGenerationConflict,
)
from app.broker_configuration.paper_reset import PaperConfigurationResetRefused, paper_configuration_reset
from app.broker_configuration.records import ConfigurationEvent, ObservedAccount
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import ProfilesStore, profiles_database_path
from app.broker_configuration.worker_lifecycle import installation_worker
from tests.broker_configuration.conftest import (
    LIVE_ENVELOPE_PAYLOAD,
    FakeAccountVerifier,
    FrozenClock,
    paper_profile,
)

ACCOUNT_ID = "PA000PAPER"


def _establish(clerk_dir: Path, clock: FrozenClock) -> Path:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=clerk_dir, clock=clock)
    path = repo.db_path
    repo.close()
    return path


def _reset(clerk_dir: Path, clock: FrozenClock, tmp_path: Path) -> None:
    developer_clean_slate_reset(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_dir,
        runner_artifacts_root=tmp_path / "runner",
        account_mode="paper",
        clock=clock,
    )


def _snapshot(clerk_dir: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(profiles_database_path(clerk_dir)) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in (
                "local_owner",
                "broker_profiles",
                "profile_revisions",
                "installation_selection",
                "account_nicknames",
                "configuration_events",
            )
        }


async def test_configuration_commit_failure_keeps_custody_reset_fenced_and_retry_finishes(
    clerk_dir: Path,
    service: BrokerConfigurationService,
    clock: FrozenClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_dir,
        clock=clock,
    )
    database = repo.db_path
    meta = repo.control_meta_snapshot()
    repo.close()
    profile_id = paper_profile(service).profile.profile_id
    await service.pin_account(profile_id, 1, account_id=ACCOUNT_ID)
    staged = service.stage_selection(profile_id=profile_id, revision=1, expected_selection_generation=0)
    service.acknowledge_effective(
        profile_id=profile_id,
        revision=1,
        account_id=ACCOUNT_ID,
        expected_selection_generation=staged.selection_generation,
    )
    service.set_nickname(ACCOUNT_ID, nickname="Disposable Paper")
    before = _snapshot(clerk_dir)
    transaction = ProfilesStore.transaction

    @contextmanager
    def fail_commit(store: ProfilesStore) -> Iterator[sqlite3.Connection]:
        with transaction(store) as conn:
            yield conn
            raise sqlite3.OperationalError("simulated configuration commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(ProfilesStore, "transaction", fail_commit)
        with pytest.raises(DeveloperCleanSlateResetRefused, match="configuration"):
            _reset(clerk_dir, clock, tmp_path)

    assert not database.exists()
    assert _snapshot(clerk_dir) == before
    registry = DeveloperCleanSlateResetRegistry(clerk_dir / "accounts" / "alpaca")
    reset_evidence = registry.path.read_bytes()
    refusal = developer_reset_refusal(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_dir,
        authority_generation=meta.authority_generation,
        db_identity_token=meta.db_identity_token,
        cutover_noun="paper",
    )
    assert refusal is not None
    assert refusal.startup_failure is not None
    assert refusal.startup_failure.reason_code == "DEVELOPER_RESET_REACTIVATION_REQUIRED"

    _reset(clerk_dir, clock, tmp_path)

    assert registry.path.read_bytes() == reset_evidence
    assert not database.exists()
    assert service.list_profiles(include_archived=True) == []
    assert service.list_nicknames() == []
    assert service.selection().effective_profile_id is None
    assert service.selection().staged_profile_id is None
    assert service.selection().selection_generation > staged.selection_generation
    assert _snapshot(clerk_dir)["configuration_events"] == before["configuration_events"]


async def test_reset_removes_paper_profiles_nicknames_and_selection(
    clerk_dir: Path, service: BrokerConfigurationService, clock: FrozenClock, tmp_path: Path
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=clerk_dir, clock=clock)
    account_database = repo.db_path
    repo.close()
    profile_id = paper_profile(service).profile.profile_id
    await service.pin_account(profile_id, 1, account_id=ACCOUNT_ID)
    staged = service.stage_selection(profile_id=profile_id, revision=1, expected_selection_generation=0)
    requested = service.request_apply(expected_selection_generation=staged.selection_generation)
    service.acknowledge_effective(
        profile_id=profile_id,
        revision=1,
        account_id=ACCOUNT_ID,
        expected_selection_generation=requested.selection_generation,
    )
    service.set_nickname(ACCOUNT_ID, nickname="Disposable Paper")
    service.close()

    developer_clean_slate_reset(
        account_id=ACCOUNT_ID,
        artifacts_root=clerk_dir,
        runner_artifacts_root=tmp_path / "runner",
        account_mode="paper",
        clock=clock,
    )

    reopened = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        assert not account_database.exists()
        assert reopened.list_profiles(include_archived=True) == []
        assert reopened.list_nicknames() == []
        selection = reopened.read_selection()
        assert selection.staged_profile_id is None
        assert selection.effective_profile_id is None
        assert not selection.apply_requested
    finally:
        reopened.close()


@pytest.mark.parametrize("selected_live", [False, True])
async def test_reset_preserves_live_other_accounts_and_immutable_history(
    clerk_dir: Path,
    service: BrokerConfigurationService,
    clock: FrozenClock,
    verifier: FakeAccountVerifier,
    tmp_path: Path,
    selected_live: bool,
) -> None:
    _establish(clerk_dir, clock)
    paper_id = paper_profile(service).profile.profile_id
    await service.pin_account(paper_id, 1, account_id=ACCOUNT_ID)
    live_id = service.create_profile(
        display_name="Keep Live",
        credential_slot="alpaca_live_primary",
        endpoint_mode="live",
        live_envelope=ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD),
    ).profile.profile_id
    await service.pin_account(live_id, 1, account_id="9LIVE0001")
    verifier.accounts.append(ObservedAccount("PAOTHER", "paper", "ACTIVE"))
    other_id = paper_profile(service, display_name="Keep other Paper").profile.profile_id
    await service.pin_account(other_id, 1, account_id="PAOTHER")
    draft_id = paper_profile(service, display_name="Unrelated draft").profile.profile_id
    for account in (ACCOUNT_ID, "PAOTHER", "9LIVE0001"):
        service.set_nickname(account, nickname=f"Name {account}")
    selected = live_id if selected_live else paper_id
    staged = service.stage_selection(
        profile_id=selected,
        revision=1,
        expected_selection_generation=0,
    )
    effective = service.acknowledge_effective(
        profile_id=selected,
        revision=1,
        account_id="9LIVE0001" if selected_live else ACCOUNT_ID,
        expected_selection_generation=staged.selection_generation,
    )
    service.request_apply(expected_selection_generation=effective.selection_generation)
    store = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        with store.transaction() as conn:
            store.append_event(
                conn,
                ConfigurationEvent(
                    event_id="retained-arming-fence",
                    actor_owner_id=service.owner().owner_id,
                    action="live_arming_invalidated",
                    profile_id=live_id,
                    revision=1,
                    previous_ref="historical-arming",
                    next_ref="configuration-change",
                    result="success",
                    recorded_at_ms=clock(),
                ),
            )
        before = _snapshot(clerk_dir)
        inode = store.db_path.stat().st_ino

        _reset(clerk_dir, clock, tmp_path)

        after = _snapshot(clerk_dir)
        assert store.db_path.stat().st_ino == inode
        assert {profile.profile_id for profile in store.list_profiles(include_archived=True)} == {
            live_id,
            other_id,
            draft_id,
        }
        assert after["profile_revisions"] == [row for row in before["profile_revisions"] if row[0] != paper_id]
        assert after["local_owner"] == before["local_owner"]
        assert after["configuration_events"] == before["configuration_events"]
        assert {row.account_id for row in store.list_nicknames()} == {"PAOTHER", "9LIVE0001"}
        selection = store.read_selection()
        assert selection.staged_profile_id == (live_id if selected_live else None)
        assert selection.effective_profile_id == (live_id if selected_live else None)
        assert selection.effective_account_id == ("9LIVE0001" if selected_live else None)
        assert selection.selection_generation > effective.selection_generation + 1
        assert not selection.apply_requested
        assert selection.apply_requested_at_ms is None
        assert selection.apply_requested_generation is None
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"), store.transaction() as conn:
            conn.execute("DELETE FROM profile_revisions WHERE profile_id = ?", (live_id,))
    finally:
        store.close()


async def test_reset_removes_only_associated_paper_revisions_from_mixed_profile(
    clerk_dir: Path,
    service: BrokerConfigurationService,
    clock: FrozenClock,
    verifier: FakeAccountVerifier,
    tmp_path: Path,
) -> None:
    _establish(clerk_dir, clock)
    profile_id = paper_profile(service).profile.profile_id
    await service.pin_account(profile_id, 1, account_id=ACCOUNT_ID)
    service.create_revision(
        profile_id,
        expected_revision=1,
        credential_slot="alpaca_paper_secondary",
        endpoint_mode="paper",
        live_envelope=None,
    )
    live = service.create_revision(
        profile_id,
        expected_revision=2,
        credential_slot="alpaca_live_primary",
        endpoint_mode="live",
        live_envelope=ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD),
    )
    verifier.accounts.append(ObservedAccount("PAOTHER", "paper", "ACTIVE"))
    service.create_revision(
        profile_id,
        expected_revision=3,
        credential_slot="alpaca_paper_primary",
        endpoint_mode="paper",
        live_envelope=None,
    )
    other = await service.pin_account(profile_id, 4, account_id="PAOTHER")
    service.create_revision(
        profile_id,
        expected_revision=4,
        credential_slot="alpaca_paper_tertiary",
        endpoint_mode="paper",
        live_envelope=None,
    )
    service.stage_selection(profile_id=profile_id, revision=5, expected_selection_generation=0)

    _reset(clerk_dir, clock, tmp_path)

    assert service.list_revisions(profile_id) == [live, other]
    assert service.selection().staged_profile_id is None
    assert service.selection().effective_profile_id is None


@pytest.mark.parametrize("owner", ["worker", "selection"])
async def test_reset_refuses_while_worker_or_selection_handover_owns_installation(
    clerk_dir: Path,
    service: BrokerConfigurationService,
    clock: FrozenClock,
    tmp_path: Path,
    owner: str,
) -> None:
    database = _establish(clerk_dir, clock)
    profile_id = paper_profile(service).profile.profile_id
    await service.pin_account(profile_id, 1, account_id=ACCOUNT_ID)
    before = _snapshot(clerk_dir)
    worker = installation_worker(clerk_dir=clerk_dir) if owner == "worker" else nullcontext()
    handover = service.selection_handover() if owner == "selection" else nullcontext()
    with worker, handover, pytest.raises(DeveloperCleanSlateResetRefused):
        _reset(clerk_dir, clock, tmp_path)
    assert database.exists()
    assert _snapshot(clerk_dir) == before


async def test_refused_custody_reset_rolls_back_configuration(
    clerk_dir: Path,
    service: BrokerConfigurationService,
    clock: FrozenClock,
    tmp_path: Path,
) -> None:
    profile_id = paper_profile(service).profile.profile_id
    await service.pin_account(profile_id, 1, account_id=ACCOUNT_ID)
    service.set_nickname(ACCOUNT_ID, nickname="Still owned")
    staged = service.stage_selection(profile_id=profile_id, revision=1, expected_selection_generation=0)
    service.request_apply(expected_selection_generation=staged.selection_generation)
    before = _snapshot(clerk_dir)

    with pytest.raises(DeveloperCleanSlateResetRefused, match="established"):
        _reset(clerk_dir, clock, tmp_path)

    assert _snapshot(clerk_dir) == before


async def test_configuration_failure_restores_deleted_rows_selection_and_retention_guard(
    clerk_dir: Path,
    service: BrokerConfigurationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_id = paper_profile(service).profile.profile_id
    await service.pin_account(profile_id, 1, account_id=ACCOUNT_ID)
    service.stage_selection(profile_id=profile_id, revision=1, expected_selection_generation=0)
    before = _snapshot(clerk_dir)
    transaction = ProfilesStore.transaction
    concurrent_snapshots = []

    def refuse_reinstallation(action: int, *_args: object) -> int:
        if action == sqlite3.SQLITE_CREATE_TRIGGER:
            concurrent_snapshots.append(_snapshot(clerk_dir))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextmanager
    def fail_guard_reinstallation(store: ProfilesStore) -> Iterator[sqlite3.Connection]:
        with transaction(store) as conn:
            conn.set_authorizer(refuse_reinstallation)
            try:
                yield conn
            finally:
                conn.set_authorizer(None)

    monkeypatch.setattr(ProfilesStore, "transaction", fail_guard_reinstallation)
    with (
        pytest.raises(ProfilesDatabaseUnavailable),
        paper_configuration_reset(account_id=ACCOUNT_ID, clerk_dir=clerk_dir),
    ):
        assert _snapshot(clerk_dir) == before

    assert _snapshot(clerk_dir) == before
    assert concurrent_snapshots == [before]
    with (
        sqlite3.connect(profiles_database_path(clerk_dir)) as conn,
        pytest.raises(sqlite3.IntegrityError, match="never deleted"),
    ):
        conn.execute("DELETE FROM profile_revisions")


async def test_reset_excludes_reentry_other_requests_database_writers_and_worker_processes(
    clerk_dir: Path,
    service: BrokerConfigurationService,
) -> None:
    profile_id = paper_profile(service).profile.profile_id
    await service.pin_account(profile_id, 1, account_id=ACCOUNT_ID)
    before = _snapshot(clerk_dir)
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "from app.broker_configuration.worker_lifecycle import installation_worker\n"
        "with installation_worker(clerk_dir=Path(sys.argv[1])) as refusal:\n"
        "    sys.exit(0 if refusal is not None else 1)\n"
    )
    with paper_configuration_reset(account_id=ACCOUNT_ID, clerk_dir=clerk_dir):
        assert _snapshot(clerk_dir) == before
        with (
            pytest.raises(PaperConfigurationResetRefused),
            paper_configuration_reset(account_id=ACCOUNT_ID, clerk_dir=clerk_dir),
        ):
            pytest.fail("a reset cannot enter its own installation fence")
        with pytest.raises(SelectionGenerationConflict):
            service.stage_selection(profile_id=profile_id, revision=1, expected_selection_generation=0)
        with (
            sqlite3.connect(profiles_database_path(clerk_dir), timeout=0) as rival,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            rival.execute("UPDATE broker_profiles SET display_name = 'racing edit'")
        result = subprocess.run(
            [sys.executable, "-c", script, str(clerk_dir)],
            capture_output=True,
            text=True,
            timeout=15,
            env={"POLYGON_API_KEY": "fake-reset-test", "DATA_PLANE_CONTROL_SECRET": ""},
        )
        assert result.returncode == 0, result.stderr

    assert _snapshot(clerk_dir)["profile_revisions"] == []
    with installation_worker(clerk_dir=clerk_dir) as refusal:
        assert refusal is None
    with service.selection_handover():
        assert not service.selection().apply_requested


def test_empty_reset_retires_bootstrap_without_owner_and_completed_retry_is_idempotent(clerk_dir: Path) -> None:
    with paper_configuration_reset(account_id=ACCOUNT_ID, clerk_dir=clerk_dir):
        assert _snapshot(clerk_dir)["local_owner"] == []
    store = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        first = store.read_selection()
        assert first.selection_generation > 0
        assert first.staged_profile_id is None
        assert first.effective_profile_id is None
        assert store.read_owner() is None
        with paper_configuration_reset(account_id=ACCOUNT_ID, clerk_dir=clerk_dir):
            assert store.read_selection() == first
        assert store.read_selection() == first
    finally:
        store.close()


def test_refused_empty_reset_does_not_retire_bootstrap_or_create_owner(clerk_dir: Path) -> None:
    with (
        pytest.raises(OSError, match="custody refusal"),
        paper_configuration_reset(account_id=ACCOUNT_ID, clerk_dir=clerk_dir),
    ):
        raise OSError("custody refusal")
    store = ProfilesStore.open(clerk_dir=clerk_dir)
    try:
        assert store.read_selection().selection_generation == 0
        assert store.read_owner() is None
    finally:
        store.close()


def test_unreadable_configuration_refuses_before_custody_operation(clerk_dir: Path) -> None:
    db_path = profiles_database_path(clerk_dir)
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"not a database")
    with (
        pytest.raises(ProfilesDatabaseUnavailable),
        paper_configuration_reset(account_id=ACCOUNT_ID, clerk_dir=clerk_dir),
    ):
        pytest.fail("unreadable configuration must refuse before custody reset")
    assert db_path.read_bytes() == b"not a database"


def test_competing_database_transaction_refuses_before_custody_operation(
    clerk_dir: Path,
    service: BrokerConfigurationService,
) -> None:
    paper_profile(service)
    before = _snapshot(clerk_dir)
    with sqlite3.connect(profiles_database_path(clerk_dir)) as rival:
        rival.execute("BEGIN IMMEDIATE")
        with (
            pytest.raises(ProfilesDatabaseUnavailable),
            paper_configuration_reset(account_id=ACCOUNT_ID, clerk_dir=clerk_dir),
        ):
            pytest.fail("reset must acquire the profiles write transaction before custody reset")
    assert _snapshot(clerk_dir) == before


def test_broken_database_link_is_not_initialized_as_a_fresh_installation(clerk_dir: Path) -> None:
    db_path = profiles_database_path(clerk_dir)
    db_path.parent.mkdir(parents=True)
    missing_database = clerk_dir / "unmounted-profiles.db"
    db_path.symlink_to(missing_database)
    with (
        pytest.raises(ProfilesDatabaseUnavailable),
        paper_configuration_reset(account_id=ACCOUNT_ID, clerk_dir=clerk_dir),
    ):
        pytest.fail("a present broken database link must refuse before custody reset")
    assert db_path.is_symlink()
    assert not missing_database.exists()


def test_missing_retention_guard_refuses_before_custody_operation(
    clerk_dir: Path,
    service: BrokerConfigurationService,
) -> None:
    paper_profile(service)
    with sqlite3.connect(profiles_database_path(clerk_dir)) as conn:
        conn.execute("DROP TRIGGER trg_profile_revisions_no_delete")
    before = _snapshot(clerk_dir)
    with (
        pytest.raises(ProfilesDatabaseUnavailable, match="retention guard"),
        paper_configuration_reset(account_id=ACCOUNT_ID, clerk_dir=clerk_dir),
    ):
        pytest.fail("missing retention evidence must refuse before custody reset")
    assert _snapshot(clerk_dir) == before


async def test_reset_does_not_reuse_removed_revision_numbers(
    clerk_dir: Path,
    service: BrokerConfigurationService,
) -> None:
    profile_id = service.create_profile(
        display_name="Mixed history",
        credential_slot="alpaca_live_primary",
        endpoint_mode="live",
        live_envelope=ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD),
    ).profile.profile_id
    service.create_revision(
        profile_id,
        expected_revision=1,
        credential_slot="alpaca_paper_primary",
        endpoint_mode="paper",
        live_envelope=None,
    )
    await service.pin_account(profile_id, 2, account_id=ACCOUNT_ID)
    with paper_configuration_reset(account_id=ACCOUNT_ID, clerk_dir=clerk_dir):
        assert [row.revision for row in service.list_revisions(profile_id)] == [1, 2]
    assert [row.revision for row in service.list_revisions(profile_id)] == [1]

    created = service.create_revision(
        profile_id,
        expected_revision=1,
        credential_slot="alpaca_paper_secondary",
        endpoint_mode="paper",
        live_envelope=None,
    )

    assert created.revision == 3
    retried = service.create_revision(
        profile_id,
        expected_revision=1,
        credential_slot="alpaca_paper_secondary",
        endpoint_mode="paper",
        live_envelope=None,
    )
    assert retried == created
    with pytest.raises(RevisionConflict):
        service.create_revision(
            profile_id,
            expected_revision=2,
            credential_slot="alpaca_paper_secondary",
            endpoint_mode="paper",
            live_envelope=None,
        )


@pytest.mark.parametrize("archived", [False, True])
async def test_any_historical_live_account_pin_refuses_before_custody_operation(
    clerk_dir: Path,
    service: BrokerConfigurationService,
    archived: bool,
) -> None:
    profile_id = service.create_profile(
        display_name="Known Live account",
        credential_slot="alpaca_live_primary",
        endpoint_mode="live",
        live_envelope=ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD),
    ).profile.profile_id
    await service.pin_account(profile_id, 1, account_id="9LIVE0001")
    service.create_revision(
        profile_id,
        expected_revision=1,
        credential_slot="alpaca_paper_primary",
        endpoint_mode="paper",
        live_envelope=None,
    )
    if archived:
        service.update_profile(profile_id, archived=True)
    before = _snapshot(clerk_dir)

    with (
        pytest.raises(PaperConfigurationResetRefused, match="Live configuration"),
        paper_configuration_reset(account_id="9LIVE0001", clerk_dir=clerk_dir),
    ):
        pytest.fail("a historical Live pin must refuse before custody reset")

    assert _snapshot(clerk_dir) == before
