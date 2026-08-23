"""Deterministic recovery and activation-fence acceptance tests for #1395."""

from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite import recovery as recovery_module
from app.broker.alpaca.clerk.sqlite import repository_lifecycle
from app.broker.alpaca.clerk.sqlite.activation import (
    ActivationRecord,
    ActivationRecordInvalid,
    ActivationStore,
)
from app.broker.alpaca.clerk.sqlite.database_verification import verify_database
from app.broker.alpaca.clerk.sqlite.mirror import MirrorChainBroken
from app.broker.alpaca.clerk.sqlite.operational_files import atomic_write_json
from app.broker.alpaca.clerk.sqlite.recovery import (
    ProcessStopProof,
    RecoveryRefusalReason,
    RecoveryRefused,
    ResetBrokerProof,
    create_verified_backup,
    preserve_and_rebuild_from_mirror,
    reset_authority,
    restore_verified_backup,
    verify_authority_head,
    verify_backup_bundle,
)
from app.broker.alpaca.clerk.sqlite.registry import EstablishedAccountsRegistry
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    IntegrityCheckFailed,
    RecoveryInProgress,
    UnsupportedWalFilesystem,
)
from app.marketdata.feed import MarketDataBar
from app.services.source_bar_ledger import SOURCE_BAR_LEDGER_FILENAME, SourceBarLedger

ACCOUNT_ID = "PA-RECOVERY"
NOW_MS = 1_800_000_000_000


def _clock() -> int:
    return NOW_MS


def _initialized(tmp_path: Path, *, account_id: str = ACCOUNT_ID) -> ClerkSqliteRepository:
    return ClerkSqliteRepository.initialize(
        account_id=account_id,
        artifacts_root=tmp_path,
        clock=_clock,
    )


def _write_activation_artifact(
    tmp_path: Path,
    *,
    path: str,
    account_id: str,
    generation: int,
    db_identity_token: str,
) -> str:
    return atomic_write_json(
        tmp_path / path,
        {
            "account_id": account_id,
            "authority_generation": generation,
            "db_identity_token": db_identity_token,
        },
    )


def test_activation_requires_matching_database_and_referenced_artifacts(tmp_path: Path) -> None:
    accounts_root = tmp_path / "accounts" / "alpaca"
    proof_path = "accounts/alpaca/PA-RECOVERY/cutover/proof.json"
    manifest_path = "accounts/alpaca/PA-RECOVERY/cutover/manifest.json"
    proof_sha = _write_activation_artifact(
        tmp_path,
        path=proof_path,
        account_id=ACCOUNT_ID,
        generation=2,
        db_identity_token="db-token",
    )
    manifest_sha = _write_activation_artifact(
        tmp_path,
        path=manifest_path,
        account_id=ACCOUNT_ID,
        generation=2,
        db_identity_token="db-token",
    )
    store = ActivationStore(accounts_root)
    record = ActivationRecord.create(
        account_id=ACCOUNT_ID,
        authority_generation=2,
        db_identity_token="db-token",
        broker_proof_reference=proof_path,
        broker_proof_sha256=proof_sha,
        legacy_quarantine_manifest=manifest_path,
        legacy_quarantine_manifest_sha256=manifest_sha,
        activated_at_ms=NOW_MS,
    )
    store.append(record)

    assert store.latest(ACCOUNT_ID) == record
    assert store.resolve(ACCOUNT_ID, 2, "db-token", artifacts_root=tmp_path) == record
    with pytest.raises(ActivationRecordInvalid, match="generation"):
        store.resolve(ACCOUNT_ID, 1, "db-token", artifacts_root=tmp_path)

    proof = tmp_path / proof_path
    outside_proof = tmp_path / "outside-proof.json"
    shutil.copyfile(proof, outside_proof)
    proof.unlink()
    proof.symlink_to(outside_proof)
    with pytest.raises(ActivationRecordInvalid, match="symbolic link"):
        store.resolve(ACCOUNT_ID, 2, "db-token", artifacts_root=tmp_path)
    proof.unlink()
    shutil.copyfile(outside_proof, proof)

    (tmp_path / proof_path).write_text("{}", encoding="utf-8")
    with pytest.raises(ActivationRecordInvalid, match="SHA-256"):
        store.resolve(ACCOUNT_ID, 2, "db-token", artifacts_root=tmp_path)


def test_activation_ledger_rejects_symbolic_link_substitution(tmp_path: Path) -> None:
    accounts_root = tmp_path / "accounts" / "alpaca"
    accounts_root.mkdir(parents=True)
    outside = tmp_path / "outside-activation.jsonl"
    outside.write_text("", encoding="utf-8")
    (accounts_root / "_authority_activations.jsonl").symlink_to(outside)

    with pytest.raises(ActivationRecordInvalid, match="symbolic link"):
        ActivationStore(accounts_root).latest(ACCOUNT_ID)


def test_interrupted_backup_keeps_previous_verified_publication(tmp_path: Path) -> None:
    repo = _initialized(tmp_path)
    repo.register_strategy_instance(strategy_instance_id="spy", symbol="SPY", config_hash="h1")
    repo.close()
    first = create_verified_backup(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock,
    )
    latest_path = first.bundle_path.parent / "latest.json"
    previous_latest = latest_path.read_bytes()

    def interrupt(_status: int, _remaining: int, _total: int) -> None:
        raise OSError("injected backup interruption")

    with pytest.raises(OSError, match="interruption"):
        create_verified_backup(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            clock=_clock,
            progress=interrupt,
        )

    assert latest_path.read_bytes() == previous_latest
    assert verify_backup_bundle(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        bundle_path=first.bundle_path,
    ) == first
    assert any(path.name.startswith(".incomplete-") for path in first.bundle_path.parent.iterdir())


def test_verify_authority_head_matches_database_and_finalized_mirror(
    tmp_path: Path,
) -> None:
    repo = _initialized(tmp_path)
    repo.register_strategy_instance(
        strategy_instance_id="spy",
        symbol="SPY",
        config_hash="h1",
    )
    repo.close()

    verification = verify_authority_head(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
    )

    assert verification.account_id == ACCOUNT_ID
    assert verification.transition_count == 1


def test_backup_creation_rejects_a_symbolic_link_publication_root(tmp_path: Path) -> None:
    repo = _initialized(tmp_path)
    account_dir = repo.db_path.parent
    repo.close()
    outside = tmp_path / "outside-backups"
    outside.mkdir()
    (account_dir / "verified-backups").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RecoveryRefused, match="must not be a symbolic link") as captured:
        create_verified_backup(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            clock=_clock,
        )
    assert captured.value.reason is RecoveryRefusalReason.OPERATIONAL_PREREQUISITE


def test_backup_verification_rejects_outside_and_symbolic_link_bundles(
    tmp_path: Path,
) -> None:
    repo = _initialized(tmp_path)
    repo.close()
    backup = create_verified_backup(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock,
    )
    outside = tmp_path / "outside-backup"
    shutil.copytree(backup.bundle_path, outside)

    with pytest.raises(RecoveryRefused, match="verified-backups root"):
        verify_backup_bundle(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            bundle_path=outside,
        )

    linked_bundle = backup.bundle_path.parent / "linked-backup"
    linked_bundle.symlink_to(backup.bundle_path, target_is_directory=True)
    with pytest.raises(RecoveryRefused, match="symbolic link"):
        verify_backup_bundle(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            bundle_path=linked_bundle,
        )


@pytest.mark.parametrize("filename", ["manifest.json", "clerk.db"])
def test_backup_verification_rejects_symbolic_link_files(
    tmp_path: Path,
    filename: str,
) -> None:
    repo = _initialized(tmp_path)
    repo.close()
    backup = create_verified_backup(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock,
    )
    target = backup.bundle_path / filename
    outside = tmp_path / f"outside-{filename}"
    shutil.copyfile(target, outside)
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(RecoveryRefused, match="non-symbolic-link file") as captured:
        verify_backup_bundle(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            bundle_path=backup.bundle_path,
        )
    assert (
        captured.value.reason
        is RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT
    )


def test_corrupt_wal_fails_closed_on_startup(tmp_path: Path) -> None:
    """A corrupt uncheckpointed WAL cannot silently discard a mirrored commit."""
    source_root = tmp_path / "source"
    clone_root = tmp_path / "clone"
    repo = _initialized(source_root)
    repo.register_strategy_instance(strategy_instance_id="spy", symbol="SPY", config_hash="h1")
    wal_path = Path(f"{repo.db_path}-wal")
    assert wal_path.is_file() and wal_path.stat().st_size > 64

    # Copy while the authority is live to model crash residue with an
    # uncheckpointed commit, including its finalized durability mirror.
    shutil.copytree(source_root / "accounts", clone_root / "accounts")
    repo.close()
    cloned_wal = clone_root / "accounts" / "alpaca" / ACCOUNT_ID / "clerk.db-wal"
    with cloned_wal.open("r+b") as handle:
        handle.seek(0)
        handle.write(b"CORRUPT-WAL" * 8)

    with pytest.raises((IntegrityCheckFailed, MirrorChainBroken)):
        ClerkSqliteRepository.open(
            account_id=ACCOUNT_ID,
            artifacts_root=clone_root,
            clock=_clock,
        )


def test_restore_rejects_tampering_and_preserves_corrupt_database(tmp_path: Path) -> None:
    repo = _initialized(tmp_path)
    repo.register_strategy_instance(strategy_instance_id="spy", symbol="SPY", config_hash="h1")
    db_path = repo.db_path
    repo.close()
    backup = create_verified_backup(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock,
    )

    original_snapshot = backup.snapshot_path.read_bytes()
    backup.snapshot_path.write_bytes(original_snapshot + b"tamper")
    with pytest.raises(RecoveryRefused, match="SHA-256") as captured:
        restore_verified_backup(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            bundle_path=backup.bundle_path,
            clock=_clock,
        )
    assert (
        captured.value.reason
        is RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT
    )
    backup.snapshot_path.write_bytes(original_snapshot)

    with db_path.open("r+b") as handle:
        handle.seek(100)
        handle.write(b"\xff" * 200)
    receipt = restore_verified_backup(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        bundle_path=backup.bundle_path,
        process_stop_proof=ProcessStopProof(
            account_id=ACCOUNT_ID,
            observed_at_ms=NOW_MS,
            proof_reference="container-supervisor:stopped",
        ),
        max_process_stop_proof_age_ms=1_000,
        clock=_clock,
    )

    assert receipt.operation == "RESTORE_VERIFIED_BACKUP"
    assert receipt.preserved_path is not None
    preserved = tmp_path / receipt.preserved_path
    assert (preserved / "clerk.db").is_file()
    verification = verify_database(db_path, expected_account_id=ACCOUNT_ID)
    assert verification.transition_count == 1


def test_restore_refuses_an_older_same_generation_snapshot(tmp_path: Path) -> None:
    repo = _initialized(tmp_path)
    repo.register_strategy_instance(strategy_instance_id="spy", symbol="SPY", config_hash="h1")
    repo.close()
    backup = create_verified_backup(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock,
    )
    reopened = ClerkSqliteRepository.open(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock,
    )
    reopened.register_strategy_instance(strategy_instance_id="qqq", symbol="QQQ", config_hash="h2")
    reopened.close()

    with pytest.raises(RecoveryRefused, match="mirror head") as captured:
        restore_verified_backup(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            bundle_path=backup.bundle_path,
            clock=_clock,
        )
    assert (
        captured.value.reason
        is RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT
    )


def test_rebuild_rejects_a_valid_mirror_from_another_account(tmp_path: Path) -> None:
    first = _initialized(tmp_path, account_id=ACCOUNT_ID)
    first.register_strategy_instance(strategy_instance_id="spy", symbol="SPY", config_hash="h1")
    first_db = first.db_path
    first_mirror = first.mirror_path
    first.close()
    second = _initialized(tmp_path, account_id="PA-OTHER")
    second.register_strategy_instance(strategy_instance_id="qqq", symbol="QQQ", config_hash="h2")
    second_mirror = second.mirror_path
    second.close()
    first_db.unlink()
    shutil.copyfile(second_mirror, first_mirror)

    with pytest.raises(MirrorChainBroken, match="does not match"):
        preserve_and_rebuild_from_mirror(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            clock=_clock,
        )
    assert not first_db.exists()


def test_rebuild_refuses_to_move_a_database_with_a_live_execution_lease(
    tmp_path: Path,
) -> None:
    repo = _initialized(tmp_path)
    repo.register_strategy_instance(strategy_instance_id="spy", symbol="SPY", config_hash="h1")

    with pytest.raises(RecoveryRefused, match="process to be stopped"):
        preserve_and_rebuild_from_mirror(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            clock=_clock,
        )

    assert repo.db_path.is_file()
    repo.close()


@pytest.mark.parametrize("operation", ["restore", "rebuild", "reset"])
def test_destructive_recovery_excludes_concurrent_authority_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    repo = _initialized(tmp_path)
    repo.register_strategy_instance(
        strategy_instance_id="spy",
        symbol="SPY",
        config_hash="h1",
    )
    repo.close()
    backup = create_verified_backup(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock,
    )
    preserve_name = (
        "_preserve_generation_files" if operation == "reset" else "_preserve_database_files"
    )
    original_preserve = getattr(recovery_module, preserve_name)
    recovery_entered = Event()
    allow_recovery = Event()

    def _pause_after_fence(*args: Any, **kwargs: Any) -> Any:
        recovery_entered.set()
        if not allow_recovery.wait(timeout=5):
            raise TimeoutError("test did not release the recovery operation")
        return original_preserve(*args, **kwargs)

    monkeypatch.setattr(recovery_module, preserve_name, _pause_after_fence)

    def _recover() -> object:
        if operation == "restore":
            return restore_verified_backup(
                account_id=ACCOUNT_ID,
                artifacts_root=tmp_path,
                bundle_path=backup.bundle_path,
                clock=_clock,
            )
        if operation == "rebuild":
            return preserve_and_rebuild_from_mirror(
                account_id=ACCOUNT_ID,
                artifacts_root=tmp_path,
                clock=_clock,
            )
        return reset_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            broker_proof=ResetBrokerProof(
                account_id=ACCOUNT_ID,
                observed_at_ms=NOW_MS,
                proof_reference="fake-broker-proof",
                positions={},
                open_order_ids=(),
            ),
            stopped_strategy_instance_ids=("spy",),
            expected_strategy_instance_ids=("spy",),
            max_proof_age_ms=1_000,
            clock=_clock,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_recover)
        assert recovery_entered.wait(timeout=5)
        try:
            with pytest.raises(RecoveryInProgress, match="fence"):
                ClerkSqliteRepository.open(
                    account_id=ACCOUNT_ID,
                    artifacts_root=tmp_path,
                    clock=_clock,
                )
        finally:
            allow_recovery.set()
        assert future.result(timeout=5) is not None


def test_rebuild_refuses_an_unreadable_database_without_process_stop_proof(
    tmp_path: Path,
) -> None:
    repo = _initialized(tmp_path)
    db_path = repo.db_path
    repo.close()
    db_path.write_bytes(b"not a sqlite database")

    with pytest.raises(RecoveryRefused, match="process-stop proof"):
        preserve_and_rebuild_from_mirror(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            clock=_clock,
        )

    assert db_path.read_bytes() == b"not a sqlite database"


def test_rebuild_accepts_fresh_process_stop_proof_for_an_unreadable_database(
    tmp_path: Path,
) -> None:
    repo = _initialized(tmp_path)
    repo.register_strategy_instance(
        strategy_instance_id="spy",
        symbol="SPY",
        config_hash="h1",
    )
    db_path = repo.db_path
    repo.close()
    db_path.write_bytes(b"not a sqlite database")

    receipt = preserve_and_rebuild_from_mirror(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        process_stop_proof=ProcessStopProof(
            account_id=ACCOUNT_ID,
            observed_at_ms=NOW_MS,
            proof_reference="container-supervisor:stopped",
        ),
        max_process_stop_proof_age_ms=1_000,
        clock=_clock,
    )

    assert receipt.process_stop_proof_reference == "container-supervisor:stopped"
    assert verify_database(db_path, expected_account_id=ACCOUNT_ID).transition_count == 1


def test_rebuild_refuses_virtiofs_before_creating_a_wal_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _initialized(tmp_path)
    db_path = repo.db_path
    repo.close()
    db_path.unlink()
    monkeypatch.setattr(
        repository_lifecycle,
        "_linux_filesystem_type",
        lambda _path: "virtiofs",
    )

    with pytest.raises(UnsupportedWalFilesystem, match="virtiofs"):
        ClerkSqliteRepository.rebuild_from_mirror(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            clock=_clock,
        )

    assert not db_path.exists()


def test_reset_refuses_virtiofs_before_preserving_the_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _initialized(tmp_path)
    db_path = repo.db_path
    repo.close()
    monkeypatch.setattr(
        repository_lifecycle,
        "_linux_filesystem_type",
        lambda _path: "virtiofs",
    )

    with pytest.raises(UnsupportedWalFilesystem, match="virtiofs"):
        reset_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            broker_proof=ResetBrokerProof(
                account_id=ACCOUNT_ID,
                observed_at_ms=NOW_MS,
                proof_reference="fake-broker-proof",
                positions={},
                open_order_ids=(),
            ),
            stopped_strategy_instance_ids=(),
            expected_strategy_instance_ids=(),
            max_proof_age_ms=1_000,
            clock=_clock,
        )

    assert db_path.is_file()


@pytest.mark.parametrize(
    ("positions", "open_orders", "message"),
    [({"SPY": 1.0}, (), "broker-flat"), ({}, ("order-1",), "no open broker orders")],
)
def test_reset_refuses_exposure_or_open_orders_without_changing_generation(
    tmp_path: Path,
    positions: dict[str, float],
    open_orders: tuple[str, ...],
    message: str,
) -> None:
    repo = _initialized(tmp_path)
    repo.close()
    proof = ResetBrokerProof(
        account_id=ACCOUNT_ID,
        observed_at_ms=NOW_MS,
        proof_reference="fake-broker-proof",
        positions=positions,
        open_order_ids=open_orders,
    )

    with pytest.raises(RecoveryRefused, match=message):
        reset_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            broker_proof=proof,
            stopped_strategy_instance_ids=(),
            expected_strategy_instance_ids=(),
            max_proof_age_ms=1_000,
            clock=_clock,
        )

    established = EstablishedAccountsRegistry(tmp_path / "accounts" / "alpaca").latest(
        ACCOUNT_ID
    )
    assert established is not None and established.authority_generation == 1


def test_reset_rejects_non_finite_position_without_moving_database(tmp_path: Path) -> None:
    repo = _initialized(tmp_path)
    db_path = repo.db_path
    repo.close()

    with pytest.raises(RecoveryRefused, match="finite"):
        reset_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            broker_proof=ResetBrokerProof(
                account_id=ACCOUNT_ID,
                observed_at_ms=NOW_MS,
                proof_reference="fake-broker-proof",
                positions={"SPY": float("nan")},
                open_order_ids=(),
            ),
            stopped_strategy_instance_ids=(),
            expected_strategy_instance_ids=(),
            max_proof_age_ms=1_000,
            clock=_clock,
        )

    assert db_path.is_file()


def test_reset_preflights_all_generation_files_before_moving_any(tmp_path: Path) -> None:
    repo = _initialized(tmp_path)
    db_path = repo.db_path
    account_dir = db_path.parent
    repo.close()
    outside = tmp_path / "outside-wal"
    outside.write_text("do not follow", encoding="utf-8")
    (account_dir / "clerk.db-wal").symlink_to(outside)

    with pytest.raises(RecoveryRefused, match="symbolic-link"):
        reset_authority(
            account_id=ACCOUNT_ID,
            artifacts_root=tmp_path,
            broker_proof=ResetBrokerProof(
                account_id=ACCOUNT_ID,
                observed_at_ms=NOW_MS,
                proof_reference="fake-broker-proof",
                positions={},
                open_order_ids=(),
            ),
            stopped_strategy_instance_ids=(),
            expected_strategy_instance_ids=(),
            max_proof_age_ms=1_000,
            clock=_clock,
        )

    assert db_path.is_file()
    assert (account_dir / "custody_transitions.mirror").is_file()


def test_flat_reset_rotates_generation_and_preserves_old_authority(tmp_path: Path) -> None:
    repo = _initialized(tmp_path)
    old_identity = repo.control_meta_snapshot().db_identity_token
    repo.register_strategy_instance(strategy_instance_id="spy", symbol="SPY", config_hash="h1")
    repo.close()
    receipt = reset_authority(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        broker_proof=ResetBrokerProof(
            account_id=ACCOUNT_ID,
            observed_at_ms=NOW_MS,
            proof_reference="fake-broker-proof",
            positions={"SPY": 0.0},
            open_order_ids=(),
        ),
        stopped_strategy_instance_ids=("spy",),
        expected_strategy_instance_ids=("spy",),
        max_proof_age_ms=1_000,
        clock=_clock,
    )

    assert receipt.authority_generation == 2
    assert receipt.db_identity_token != old_identity
    assert receipt.preserved_path is not None
    preserved = tmp_path / receipt.preserved_path
    assert (preserved / "clerk.db").is_file()
    assert (preserved / "custody_transitions.mirror").is_file()
    verification = verify_database(
        tmp_path / "accounts" / "alpaca" / ACCOUNT_ID / "clerk.db",
        expected_account_id=ACCOUNT_ID,
        expected_generation=2,
        expected_db_identity=receipt.db_identity_token,
    )
    assert verification.transition_count == 0
    assert json.loads((preserved / "manifest.json").read_text())["authority_generation"] == 2


# ---------------------------------------------------------------------------
# #1740: the verified backup bundle carries the retained source-bar ledger.
#
# #1727 states that "retained bars plus durable Clerk dispositions are
# canonical recovery evidence; provider re-fetch is never accepted as
# recovery evidence". A bundle that restored dispositions without the bars
# they were decided on restored half the evidence.
# ---------------------------------------------------------------------------


def _market_bar(symbol: str, end_ms: int, close: str) -> MarketDataBar:
    price = Decimal(close)
    return MarketDataBar(
        symbol=symbol,
        start_ms=end_ms - 60_000,
        end_ms=end_ms,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1,
        fetched_at_ms=end_ms,
        feed_id="test-recovery",
        session_phase="RTH",
    )


def _ledger_with_bars(tmp_path: Path, *, closes: list[str]) -> Path:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT_ID)
    for index, close in enumerate(closes):
        ledger.append_history(_market_bar("SPY", NOW_MS + (index + 1) * 60_000, close))
    ledger.close()
    return ledger.path


def _ledger_closes(tmp_path: Path) -> list[str]:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT_ID)
    try:
        return [str(bar.close) for bar in ledger.bars(provider="test-recovery", symbol="SPY")]
    finally:
        ledger.close()


def _stopped_restore(tmp_path: Path, bundle_path: Path) -> recovery_module.RecoveryReceipt:
    return restore_verified_backup(
        account_id=ACCOUNT_ID,
        artifacts_root=tmp_path,
        bundle_path=bundle_path,
        process_stop_proof=ProcessStopProof(
            account_id=ACCOUNT_ID,
            observed_at_ms=NOW_MS,
            proof_reference="container-supervisor:stopped",
        ),
        max_process_stop_proof_age_ms=1_000,
        clock=_clock,
    )


def test_backup_bundle_carries_the_source_bar_ledger_and_restore_brings_it_back(tmp_path: Path) -> None:
    repo = _initialized(tmp_path)
    repo.close()
    ledger_path = _ledger_with_bars(tmp_path, closes=["100", "101"])

    backup = create_verified_backup(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock)

    assert backup.source_bars is not None
    assert backup.source_bars.path == backup.bundle_path / SOURCE_BAR_LEDGER_FILENAME
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["source_bars"] == {
        "filename": SOURCE_BAR_LEDGER_FILENAME,
        "sha256": backup.source_bars.sha256,
        "retained_rows": 2,
    }
    verified = verify_backup_bundle(account_id=ACCOUNT_ID, artifacts_root=tmp_path, bundle_path=backup.bundle_path)
    assert verified.source_bars == backup.source_bars

    # The live ledger moves on after the backup; a restore must bring the
    # ledger back to the bundle's cut and keep the newer ledger as evidence.
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id=ACCOUNT_ID)
    ledger.append(_market_bar("SPY", NOW_MS + 3 * 60_000, "102"))
    ledger.close()
    assert _ledger_closes(tmp_path) == ["100", "101", "102"]

    receipt = _stopped_restore(tmp_path, backup.bundle_path)

    assert _ledger_closes(tmp_path) == ["100", "101"]
    assert receipt.preserved_path is not None
    preserved = tmp_path / receipt.preserved_path
    assert (preserved / "clerk.db").is_file()
    assert (preserved / SOURCE_BAR_LEDGER_FILENAME).is_file()
    assert ledger_path.is_file()


def test_backup_without_a_source_bar_ledger_records_the_absence_and_restore_creates_none(
    tmp_path: Path,
) -> None:
    repo = _initialized(tmp_path)
    repo.close()

    backup = create_verified_backup(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock)

    assert backup.source_bars is None
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["source_bars"] is None
    _stopped_restore(tmp_path, backup.bundle_path)
    _accounts_root, account_dir = recovery_module.writes.account_paths(tmp_path, ACCOUNT_ID)
    assert not (account_dir / SOURCE_BAR_LEDGER_FILENAME).exists()


def _tamper_snapshot_bytes(backup: recovery_module.BackupPublication) -> None:
    assert backup.source_bars is not None
    backup.source_bars.path.write_bytes(backup.source_bars.path.read_bytes() + b"tamper")


def _tamper_manifest_row_count(backup: recovery_module.BackupPublication) -> None:
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    manifest["source_bars"]["retained_rows"] += 1
    backup.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _tamper_manifest_shape(backup: recovery_module.BackupPublication) -> None:
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    del manifest["source_bars"]["retained_rows"]
    backup.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _swap_snapshot_for_symlink(backup: recovery_module.BackupPublication) -> None:
    assert backup.source_bars is not None
    real = backup.source_bars.path.with_name("elsewhere.sqlite3")
    backup.source_bars.path.rename(real)
    backup.source_bars.path.symlink_to(real)


@pytest.mark.parametrize(
    ("tamper", "match"),
    [
        (_tamper_snapshot_bytes, "source-bar snapshot SHA-256"),
        (_tamper_manifest_row_count, "row count does not match"),
        (_tamper_manifest_shape, "source_bars entry is unsupported"),
        (_swap_snapshot_for_symlink, "regular non-symbolic-link file"),
    ],
    ids=["snapshot-bytes", "manifest-row-count", "manifest-shape", "symlink"],
)
def test_restore_refuses_a_tampered_source_bar_snapshot(
    tmp_path: Path,
    tamper: Any,
    match: str,
) -> None:
    repo = _initialized(tmp_path)
    repo.close()
    _ledger_with_bars(tmp_path, closes=["100"])
    backup = create_verified_backup(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock)
    tamper(backup)

    with pytest.raises(RecoveryRefused, match=match):
        _stopped_restore(tmp_path, backup.bundle_path)

    assert _ledger_closes(tmp_path) == ["100"]


def test_schema_version_1_bundle_still_restores_dispositions_and_leaves_the_ledger_alone(
    tmp_path: Path,
) -> None:
    """Bundles published before #1740 carry no ledger. They remain restorable
    (dispositions only), and a restore from one must neither replace nor
    preserve the live ledger -- the absence of a ledger in the bundle is not
    evidence that there should be none."""
    repo = _initialized(tmp_path)
    repo.close()
    _ledger_with_bars(tmp_path, closes=["100"])
    backup = create_verified_backup(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock)
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    del manifest["source_bars"]
    manifest["schema_version"] = 1
    assert backup.source_bars is not None
    backup.source_bars.path.unlink()
    backup.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verified = verify_backup_bundle(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, bundle_path=backup.bundle_path
    )
    assert verified.source_bars is None
    receipt = _stopped_restore(tmp_path, backup.bundle_path)

    assert receipt.operation == "RESTORE_VERIFIED_BACKUP"
    assert _ledger_closes(tmp_path) == ["100"]
    assert receipt.preserved_path is not None
    assert not (tmp_path / receipt.preserved_path / SOURCE_BAR_LEDGER_FILENAME).exists()
