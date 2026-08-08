"""Explicit backup, restore, rebuild, and reset workflows for Clerk SQLite.

These operations are intentionally absent from normal startup.  Every input is
verified first, old files are moved to a diagnostic archive rather than
overwritten, and an immutable receipt records what became active.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.sqlite import schema, writes
from app.broker.alpaca.clerk.sqlite.activation import ActivationRecord, ActivationStore
from app.broker.alpaca.clerk.sqlite.database_verification import (
    DatabaseVerification,
    sha256_file,
    verify_database,
)
from app.broker.alpaca.clerk.sqlite.mirror import MirrorFile, MirrorIdentity
from app.broker.alpaca.clerk.sqlite.operational_files import (
    atomic_write_json,
    canonical_json_bytes,
    relative_reference,
)
from app.broker.alpaca.clerk.sqlite.registry import (
    EstablishedAccountsRegistry,
    EstablishedGeneration,
)
from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME, MIRROR_FILENAME, ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.repository_lifecycle import (
    assert_wal_filesystem_supported,
    exclusive_recovery_fence,
    startup_recovery_fence,
)
from app.broker.alpaca.paths import fsync_directory, fsync_directory_chain
from app.utils.timestamps import Clock, now_ms_utc

BACKUP_DIRECTORY = "verified-backups"
LATEST_BACKUP_FILENAME = "latest.json"
PRESERVED_DIRECTORY = "recovery-preserved"
RECEIPT_DIRECTORY = "recovery-receipts"


class RecoveryRefusalReason(StrEnum):
    """Machine-readable reason for refusing a recovery operation."""

    OPERATIONAL_PREREQUISITE = "OPERATIONAL_PREREQUISITE"
    INTEGRITY_OR_IDENTITY_DISAGREEMENT = "INTEGRITY_OR_IDENTITY_DISAGREEMENT"


class RecoveryRefused(Exception):
    """Recovery prerequisites are absent, stale, or identity-mismatched."""

    def __init__(
        self,
        message: str,
        *,
        reason: RecoveryRefusalReason = RecoveryRefusalReason.OPERATIONAL_PREREQUISITE,
    ) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class BackupPublication:
    bundle_path: Path
    manifest_path: Path
    snapshot_path: Path
    snapshot_sha256: str
    verification: DatabaseVerification


@dataclass(frozen=True)
class RecoveryReceipt:
    operation: str
    account_id: str
    authority_generation: int
    db_identity_token: str
    proof_reference: str
    preserved_path: str | None
    recorded_at_ms: int
    process_stop_proof_reference: str | None = None


@dataclass(frozen=True)
class ResetBrokerProof:
    """Fresh broker/account evidence required for destructive generation rotation."""

    account_id: str
    observed_at_ms: int
    proof_reference: str
    positions: Mapping[str, float]
    open_order_ids: Sequence[str]


@dataclass(frozen=True)
class ProcessStopProof:
    """Fresh independent evidence that the authority writer is stopped."""

    account_id: str
    observed_at_ms: int
    proof_reference: str


@contextmanager
def _atomic_recovery_fence(
    *,
    account_id: str,
    artifacts_root: Path,
) -> Iterator[None]:
    """Hold the stable account fence across every destructive file publication."""
    from app.broker.alpaca.clerk.sqlite.repository import RecoveryInProgress

    try:
        with exclusive_recovery_fence(
            artifacts_root=artifacts_root,
            account_id=account_id,
        ):
            yield
    except RecoveryInProgress as exc:
        raise RecoveryRefused(
            f"account {account_id!r} startup or recovery is already in progress"
        ) from exc


def create_verified_backup(
    *,
    account_id: str,
    artifacts_root: Path,
    clock: Clock = now_ms_utc,
    progress: Callable[[int, int, int], None] | None = None,
) -> BackupPublication:
    """Publish a WAL-safe online-backup bundle after complete verification."""
    with startup_recovery_fence(
        artifacts_root=artifacts_root,
        account_id=account_id,
    ):
        return _create_verified_backup_fenced(
            account_id=account_id,
            artifacts_root=artifacts_root,
            clock=clock,
            progress=progress,
        )


def _create_verified_backup_fenced(
    *,
    account_id: str,
    artifacts_root: Path,
    clock: Clock,
    progress: Callable[[int, int, int], None] | None,
) -> BackupPublication:
    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    established = _require_established(accounts_root, account_id)
    db_path = writes.confined_account_file(artifacts_root, account_id, DB_FILENAME)
    source_verification = verify_database(
        db_path,
        expected_account_id=account_id,
        expected_generation=established.authority_generation,
        expected_db_identity=established.db_identity_token,
    )
    backup_root = account_dir / BACKUP_DIRECTORY
    if backup_root.is_symlink():
        raise RecoveryRefused("verified backup root must not be a symbolic link")
    backup_root.mkdir(parents=True, exist_ok=True)
    fsync_directory_chain(backup_root, artifacts_root)
    candidate = Path(tempfile.mkdtemp(prefix=".incomplete-", dir=backup_root))
    snapshot = candidate / DB_FILENAME
    try:
        _online_backup(db_path, snapshot, progress=progress)
        verification = verify_database(
            snapshot,
            expected_account_id=account_id,
            expected_generation=source_verification.authority_generation,
            expected_db_identity=source_verification.db_identity_token,
        )
        if verification != source_verification:
            raise RecoveryRefused(
                "online backup does not match the verified source cut",
                reason=RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT,
            )
        snapshot_sha256 = sha256_file(snapshot)
        manifest = {
            "schema_version": 1,
            "account_id": account_id,
            "authority_generation": verification.authority_generation,
            "db_identity_token": verification.db_identity_token,
            "created_at_ms": clock(),
            "snapshot_filename": DB_FILENAME,
            "snapshot_sha256": snapshot_sha256,
            "verification": asdict(verification),
        }
        manifest_path = candidate / "manifest.json"
        manifest_sha256 = atomic_write_json(manifest_path, manifest)
        fsync_directory(candidate)
        bundle_name = (
            f"backup-g{verification.authority_generation}-{manifest['created_at_ms']}-"
            f"{snapshot_sha256[:16]}"
        )
        bundle = backup_root / bundle_name
        if bundle.exists():
            raise RecoveryRefused(f"verified backup bundle already exists: {bundle}")
        os.replace(candidate, bundle)
        fsync_directory(backup_root)
        latest_payload = {
            "schema_version": 1,
            "account_id": account_id,
            "authority_generation": verification.authority_generation,
            "bundle": bundle_name,
            "manifest_sha256": manifest_sha256,
        }
        atomic_write_json(backup_root / LATEST_BACKUP_FILENAME, latest_payload)
        return BackupPublication(
            bundle_path=bundle,
            manifest_path=bundle / "manifest.json",
            snapshot_path=bundle / DB_FILENAME,
            snapshot_sha256=snapshot_sha256,
            verification=verification,
        )
    except Exception:
        # The incomplete directory is retained and never selected by latest.json.
        # It is forensic evidence for disk-full/interruption diagnosis.
        raise


def verify_backup_bundle(
    *,
    account_id: str,
    artifacts_root: Path,
    bundle_path: Path,
) -> BackupPublication:
    """Reject incomplete, tampered, wrong-account, and wrong-generation bundles."""
    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    established = _require_established(accounts_root, account_id)
    bundle = _resolve_published_backup_bundle(
        account_dir=account_dir,
        bundle_path=bundle_path,
    )
    if not bundle.is_dir() or bundle.name.startswith(".incomplete-"):
        raise RecoveryRefused("backup must be a published verified bundle")
    manifest_path = bundle / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RecoveryRefused(
            "backup manifest must be a regular non-symbolic-link file",
            reason=RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT,
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RecoveryRefused(f"backup manifest cannot be read: {exc}") from exc
    required = {
        "schema_version",
        "account_id",
        "authority_generation",
        "db_identity_token",
        "created_at_ms",
        "snapshot_filename",
        "snapshot_sha256",
        "verification",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise RecoveryRefused("backup manifest fields do not match schema version 1")
    if manifest["schema_version"] != 1 or manifest["snapshot_filename"] != DB_FILENAME:
        raise RecoveryRefused("backup manifest schema or snapshot filename is unsupported")
    if manifest["account_id"] != account_id:
        raise RecoveryRefused(
            "backup belongs to a different account",
            reason=RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT,
        )
    if manifest["authority_generation"] != established.authority_generation:
        raise RecoveryRefused(
            "backup belongs to a different authority generation",
            reason=RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT,
        )
    if manifest["db_identity_token"] != established.db_identity_token:
        raise RecoveryRefused(
            "backup belongs to a different database identity",
            reason=RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT,
        )
    snapshot = bundle / DB_FILENAME
    if snapshot.is_symlink() or not snapshot.is_file():
        raise RecoveryRefused(
            "backup snapshot must be a regular non-symbolic-link file",
            reason=RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT,
        )
    if sha256_file(snapshot) != manifest["snapshot_sha256"]:
        raise RecoveryRefused(
            "backup snapshot SHA-256 does not match its manifest",
            reason=RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT,
        )
    verification = verify_database(
        snapshot,
        expected_account_id=account_id,
        expected_generation=established.authority_generation,
        expected_db_identity=established.db_identity_token,
    )
    if asdict(verification) != manifest["verification"]:
        raise RecoveryRefused(
            "backup verification receipt does not match snapshot contents",
            reason=RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT,
        )
    return BackupPublication(
        bundle_path=bundle,
        manifest_path=manifest_path,
        snapshot_path=snapshot,
        snapshot_sha256=manifest["snapshot_sha256"],
        verification=verification,
    )


def verify_authority_head(
    *,
    account_id: str,
    artifacts_root: Path,
) -> DatabaseVerification:
    """Verify one stopped authority database agrees with its finalized mirror head."""
    with startup_recovery_fence(
        artifacts_root=artifacts_root,
        account_id=account_id,
    ):
        return _verify_authority_head_fenced(
            account_id=account_id,
            artifacts_root=artifacts_root,
        )


def _verify_authority_head_fenced(
    *,
    account_id: str,
    artifacts_root: Path,
) -> DatabaseVerification:
    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    assert_wal_filesystem_supported(account_dir)
    established = _require_established(accounts_root, account_id)
    verification = verify_database(
        account_dir / DB_FILENAME,
        expected_account_id=account_id,
        expected_generation=established.authority_generation,
        expected_db_identity=established.db_identity_token,
    )
    _verify_snapshot_matches_mirror_head(
        account_id=account_id,
        account_dir=account_dir,
        established=established,
        verification=verification,
    )
    return verification


def _resolve_published_backup_bundle(*, account_dir: Path, bundle_path: Path) -> Path:
    """Resolve one direct bundle child without following caller-controlled links."""
    backup_root = account_dir / BACKUP_DIRECTORY
    if backup_root.is_symlink() or not backup_root.is_dir():
        raise RecoveryRefused("verified backup root must be a regular directory")
    normalized_root = Path(os.path.abspath(backup_root))
    normalized_bundle = Path(os.path.abspath(bundle_path))
    if normalized_bundle.parent != normalized_root:
        raise RecoveryRefused("backup bundle must be inside this account's verified-backups root")
    if normalized_bundle.is_symlink():
        raise RecoveryRefused("backup bundle must not be a symbolic link")
    try:
        resolved_root = normalized_root.resolve(strict=True)
        resolved_bundle = normalized_bundle.resolve(strict=True)
    except OSError as exc:
        raise RecoveryRefused(f"backup bundle cannot be resolved: {exc}") from exc
    if resolved_bundle.parent != resolved_root:
        raise RecoveryRefused("backup bundle escapes this account's verified-backups root")
    return resolved_bundle


def restore_verified_backup(
    *,
    account_id: str,
    artifacts_root: Path,
    bundle_path: Path,
    process_stop_proof: ProcessStopProof | None = None,
    max_process_stop_proof_age_ms: int = 120_000,
    clock: Clock = now_ms_utc,
) -> RecoveryReceipt:
    """Restore only a same-generation verified bundle and preserve old files."""
    with _atomic_recovery_fence(
        account_id=account_id,
        artifacts_root=artifacts_root,
    ):
        return _restore_verified_backup_fenced(
            account_id=account_id,
            artifacts_root=artifacts_root,
            bundle_path=bundle_path,
            process_stop_proof=process_stop_proof,
            max_process_stop_proof_age_ms=max_process_stop_proof_age_ms,
            clock=clock,
        )


def _restore_verified_backup_fenced(
    *,
    account_id: str,
    artifacts_root: Path,
    bundle_path: Path,
    process_stop_proof: ProcessStopProof | None,
    max_process_stop_proof_age_ms: int,
    clock: Clock,
) -> RecoveryReceipt:
    publication = verify_backup_bundle(
        account_id=account_id,
        artifacts_root=artifacts_root,
        bundle_path=bundle_path,
    )
    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    assert_wal_filesystem_supported(account_dir)
    established = _require_established(accounts_root, account_id)
    _verify_snapshot_matches_mirror_head(
        account_id=account_id,
        account_dir=account_dir,
        established=established,
        verification=publication.verification,
    )
    db_path = writes.confined_account_file(artifacts_root, account_id, DB_FILENAME)
    recorded_at_ms = clock()
    process_stop_reference = _assert_no_live_lease(
        db_path,
        account_id=account_id,
        now_ms=recorded_at_ms,
        process_stop_proof=process_stop_proof,
        max_process_stop_proof_age_ms=max_process_stop_proof_age_ms,
    )
    candidate = account_dir / f".{DB_FILENAME}.restore-{secrets.token_hex(8)}"
    shutil.copyfile(publication.snapshot_path, candidate)
    _fsync_file(candidate)
    verify_database(
        candidate,
        expected_account_id=account_id,
        expected_generation=publication.verification.authority_generation,
        expected_db_identity=publication.verification.db_identity_token,
    )
    preserved = _preserve_database_files(
        account_dir=account_dir,
        recorded_at_ms=recorded_at_ms,
        label="pre-restore",
    )
    try:
        os.replace(candidate, db_path)
        fsync_directory(account_dir)
    except Exception:
        candidate.unlink(missing_ok=True)
        _restore_preserved_database(account_dir=account_dir, preserved=preserved)
        raise
    receipt = RecoveryReceipt(
        operation="RESTORE_VERIFIED_BACKUP",
        account_id=account_id,
        authority_generation=publication.verification.authority_generation,
        db_identity_token=publication.verification.db_identity_token,
        proof_reference=relative_reference(artifacts_root, publication.manifest_path),
        preserved_path=relative_reference(artifacts_root, preserved) if preserved else None,
        recorded_at_ms=recorded_at_ms,
        process_stop_proof_reference=process_stop_reference,
    )
    _write_recovery_receipt(account_dir, receipt)
    return receipt


def preserve_and_rebuild_from_mirror(
    *,
    account_id: str,
    artifacts_root: Path,
    process_stop_proof: ProcessStopProof | None = None,
    max_process_stop_proof_age_ms: int = 120_000,
    clock: Clock = now_ms_utc,
) -> RecoveryReceipt:
    """Verify a bound contiguous mirror, preserve old DB files, then replay."""
    with _atomic_recovery_fence(
        account_id=account_id,
        artifacts_root=artifacts_root,
    ):
        return _preserve_and_rebuild_from_mirror_fenced(
            account_id=account_id,
            artifacts_root=artifacts_root,
            process_stop_proof=process_stop_proof,
            max_process_stop_proof_age_ms=max_process_stop_proof_age_ms,
            clock=clock,
        )


def _preserve_and_rebuild_from_mirror_fenced(
    *,
    account_id: str,
    artifacts_root: Path,
    process_stop_proof: ProcessStopProof | None,
    max_process_stop_proof_age_ms: int,
    clock: Clock,
) -> RecoveryReceipt:
    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    assert_wal_filesystem_supported(account_dir)
    established = _require_established(accounts_root, account_id)
    mirror_path = writes.confined_account_file(artifacts_root, account_id, MIRROR_FILENAME)
    mirror = MirrorFile(
        mirror_path,
        expected_identity=MirrorIdentity(
            account_id=account_id,
            authority_generation=established.authority_generation,
            db_identity_token=established.db_identity_token,
        ),
    )
    rows = mirror.rebuild()
    if any(row.authority_generation != established.authority_generation for row in rows):
        raise RecoveryRefused(
            "mirror contains rows from a different generation",
            reason=RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT,
        )
    recorded_at_ms = clock()
    process_stop_reference = _assert_no_live_lease(
        account_dir / DB_FILENAME,
        account_id=account_id,
        now_ms=recorded_at_ms,
        process_stop_proof=process_stop_proof,
        max_process_stop_proof_age_ms=max_process_stop_proof_age_ms,
    )
    preserved = _preserve_database_files(
        account_dir=account_dir,
        recorded_at_ms=recorded_at_ms,
        label="pre-rebuild",
    )
    try:
        rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
            account_id=account_id,
            artifacts_root=artifacts_root,
            clock=clock,
        )
        rebuilt.close()
        verification = verify_database(
            account_dir / DB_FILENAME,
            expected_account_id=account_id,
            expected_generation=established.authority_generation,
            expected_db_identity=established.db_identity_token,
        )
    except Exception:
        _restore_preserved_database(account_dir=account_dir, preserved=preserved)
        raise
    receipt = RecoveryReceipt(
        operation="REBUILD_FROM_MIRROR",
        account_id=account_id,
        authority_generation=verification.authority_generation,
        db_identity_token=verification.db_identity_token,
        proof_reference=relative_reference(artifacts_root, mirror_path),
        preserved_path=relative_reference(artifacts_root, preserved) if preserved else None,
        recorded_at_ms=recorded_at_ms,
        process_stop_proof_reference=process_stop_reference,
    )
    _write_recovery_receipt(account_dir, receipt)
    return receipt


def reset_authority(
    *,
    account_id: str,
    artifacts_root: Path,
    broker_proof: ResetBrokerProof,
    stopped_strategy_instance_ids: Sequence[str],
    expected_strategy_instance_ids: Sequence[str],
    max_proof_age_ms: int,
    process_stop_proof: ProcessStopProof | None = None,
    max_process_stop_proof_age_ms: int = 120_000,
    clock: Clock = now_ms_utc,
) -> RecoveryReceipt:
    """Rotate to a fresh empty generation only from current flat broker proof."""
    with _atomic_recovery_fence(
        account_id=account_id,
        artifacts_root=artifacts_root,
    ):
        return _reset_authority_fenced(
            account_id=account_id,
            artifacts_root=artifacts_root,
            broker_proof=broker_proof,
            stopped_strategy_instance_ids=stopped_strategy_instance_ids,
            expected_strategy_instance_ids=expected_strategy_instance_ids,
            max_proof_age_ms=max_proof_age_ms,
            process_stop_proof=process_stop_proof,
            max_process_stop_proof_age_ms=max_process_stop_proof_age_ms,
            clock=clock,
        )


def _reset_authority_fenced(
    *,
    account_id: str,
    artifacts_root: Path,
    broker_proof: ResetBrokerProof,
    stopped_strategy_instance_ids: Sequence[str],
    expected_strategy_instance_ids: Sequence[str],
    max_proof_age_ms: int,
    process_stop_proof: ProcessStopProof | None,
    max_process_stop_proof_age_ms: int,
    clock: Clock,
) -> RecoveryReceipt:
    now = clock()
    _validate_reset_proof(
        account_id=account_id,
        proof=broker_proof,
        now_ms=now,
        max_proof_age_ms=max_proof_age_ms,
        stopped_strategy_instance_ids=stopped_strategy_instance_ids,
        expected_strategy_instance_ids=expected_strategy_instance_ids,
    )
    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    assert_wal_filesystem_supported(account_dir)
    established = _require_established(accounts_root, account_id)
    _preflight_regular_files(
        account_dir,
        (DB_FILENAME, f"{DB_FILENAME}-wal", f"{DB_FILENAME}-shm", MIRROR_FILENAME),
        label="generation",
    )
    process_stop_reference = _assert_no_live_lease(
        account_dir / DB_FILENAME,
        account_id=account_id,
        now_ms=now,
        process_stop_proof=process_stop_proof,
        max_process_stop_proof_age_ms=max_process_stop_proof_age_ms,
    )
    previous_activation = ActivationStore(accounts_root).latest(account_id)
    preserved = _preserve_generation_files(
        account_dir=account_dir,
        recorded_at_ms=now,
        generation=established.authority_generation,
    )
    next_generation = established.authority_generation + 1
    db_identity_token = secrets.token_hex(16)
    provenance = {
        "operation": "RESET_AUTHORITY",
        "account_id": account_id,
        "prior_generation": established.authority_generation,
        "authority_generation": next_generation,
        "db_identity_token": db_identity_token,
        "broker_proof_reference": broker_proof.proof_reference,
        "broker_observed_at_ms": broker_proof.observed_at_ms,
        "recorded_at_ms": now,
        "preserved_path": relative_reference(artifacts_root, preserved),
    }
    try:
        _initialize_reset_generation(
            account_id=account_id,
            account_dir=account_dir,
            authority_generation=next_generation,
            db_identity_token=db_identity_token,
            reset_provenance=provenance,
            now_ms=now,
        )
        EstablishedAccountsRegistry(accounts_root).record(
            EstablishedGeneration(
                account_id=account_id,
                authority_generation=next_generation,
                db_identity_token=db_identity_token,
                established_at_ms=now,
            )
        )
        preserved_manifest = preserved / "manifest.json"
        preserved_payload = json.loads(preserved_manifest.read_text(encoding="utf-8"))
        preserved_payload.update(
            {
                "account_id": account_id,
                "authority_generation": next_generation,
                "db_identity_token": db_identity_token,
                "preserved_authority_generation": established.authority_generation,
                "preserved_db_identity_token": preserved_payload["db_identity_token"],
            }
        )
        atomic_write_json(preserved_manifest, preserved_payload)
    except Exception:
        # A registry append is intentionally irreversible. If it succeeded but
        # later publication failed, startup sees a mismatch and stays closed.
        raise

    receipt = RecoveryReceipt(
        operation="RESET_AUTHORITY",
        account_id=account_id,
        authority_generation=next_generation,
        db_identity_token=db_identity_token,
        proof_reference=broker_proof.proof_reference,
        preserved_path=relative_reference(artifacts_root, preserved),
        recorded_at_ms=now,
        process_stop_proof_reference=process_stop_reference,
    )
    _write_recovery_receipt(account_dir, receipt)
    if previous_activation is not None:
        proof_path = account_dir / RECEIPT_DIRECTORY / f"reset-proof-g{next_generation}.json"
        proof_payload = {
            **provenance,
            "positions": dict(sorted(broker_proof.positions.items())),
            "open_order_ids": sorted(broker_proof.open_order_ids),
        }
        proof_sha = atomic_write_json(proof_path, proof_payload)
        quarantine_manifest = preserved / "manifest.json"
        quarantine_sha = sha256_file(quarantine_manifest)
        ActivationStore(accounts_root).append(
            ActivationRecord.create(
                account_id=account_id,
                authority_generation=next_generation,
                db_identity_token=db_identity_token,
                broker_proof_reference=relative_reference(artifacts_root, proof_path),
                broker_proof_sha256=proof_sha,
                legacy_quarantine_manifest=relative_reference(
                    artifacts_root, quarantine_manifest
                ),
                legacy_quarantine_manifest_sha256=quarantine_sha,
                activated_at_ms=now,
            )
        )
    return receipt


def _online_backup(
    source_path: Path,
    destination_path: Path,
    *,
    progress: Callable[[int, int, int], None] | None,
) -> None:
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(f"{source_path.resolve().as_uri()}?mode=ro", uri=True)
        destination = sqlite3.connect(destination_path)
        source.backup(destination, pages=64, progress=progress)
        destination.commit()
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
    _fsync_file(destination_path)


def _require_established(
    accounts_root: Path, account_id: str
) -> EstablishedGeneration:
    established = EstablishedAccountsRegistry(accounts_root).latest(account_id)
    if established is None:
        raise RecoveryRefused(f"account {account_id!r} has no established SQLite authority")
    return established


def _verify_snapshot_matches_mirror_head(
    *,
    account_id: str,
    account_dir: Path,
    established: EstablishedGeneration,
    verification: DatabaseVerification,
) -> None:
    mirror = MirrorFile(
        account_dir / MIRROR_FILENAME,
        expected_identity=MirrorIdentity(
            account_id=account_id,
            authority_generation=established.authority_generation,
            db_identity_token=established.db_identity_token,
        ),
    )
    rows = mirror.rebuild()
    mirror_sequence = rows[-1].sequence if rows else 0
    mirror_hash = rows[-1].row_hash if rows else "GENESIS"
    if (verification.last_sequence, verification.last_row_hash) != (
        mirror_sequence,
        mirror_hash,
    ):
        raise RecoveryRefused(
            "backup is not at the current finalized mirror head; refusing generation rollback",
            reason=RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT,
        )


def _assert_no_live_lease(
    db_path: Path,
    *,
    account_id: str,
    now_ms: int,
    process_stop_proof: ProcessStopProof | None,
    max_process_stop_proof_age_ms: int,
) -> str | None:
    if not db_path.is_file():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT execution_lease_owner, execution_lease_expires_at_ms "
            "FROM control_meta WHERE id = 1"
        ).fetchone()
    except sqlite3.DatabaseError:
        _validate_process_stop_proof(
            account_id=account_id,
            proof=process_stop_proof,
            now_ms=now_ms,
            max_proof_age_ms=max_process_stop_proof_age_ms,
        )
        assert process_stop_proof is not None
        return process_stop_proof.proof_reference
    finally:
        if conn is not None:
            conn.close()
    if row is not None and row[0] is not None and row[1] is not None and row[1] >= now_ms:
        raise RecoveryRefused("authority recovery requires the active Clerk process to be stopped")
    return None


def _validate_process_stop_proof(
    *,
    account_id: str,
    proof: ProcessStopProof | None,
    now_ms: int,
    max_proof_age_ms: int,
) -> None:
    if proof is None:
        raise RecoveryRefused(
            "the authority database is unreadable; fresh independent process-stop proof is required"
        )
    if (
        proof.account_id != account_id
        or type(proof.observed_at_ms) is not int
        or not isinstance(proof.proof_reference, str)
        or not proof.proof_reference
    ):
        raise RecoveryRefused("process-stop proof identity or fields are invalid")
    if (
        type(max_proof_age_ms) is not int
        or max_proof_age_ms < 1
        or proof.observed_at_ms > now_ms
    ):
        raise RecoveryRefused("process-stop proof timestamp or freshness policy is invalid")
    if now_ms - proof.observed_at_ms > max_proof_age_ms:
        raise RecoveryRefused("process-stop proof is stale")


def _preserve_database_files(
    *, account_dir: Path, recorded_at_ms: int, label: str
) -> Path | None:
    names = (DB_FILENAME, f"{DB_FILENAME}-wal", f"{DB_FILENAME}-shm")
    existing = _preflight_regular_files(account_dir, names, label="database")
    if not existing:
        return None
    destination = (
        account_dir
        / PRESERVED_DIRECTORY
        / f"{label}-{recorded_at_ms}-{secrets.token_hex(4)}"
    )
    destination.mkdir(parents=True, exist_ok=False)
    fsync_directory_chain(destination, account_dir)
    for source in existing:
        os.replace(source, destination / source.name)
    atomic_write_json(
        destination / "manifest.json",
        {
            "schema_version": 1,
            "label": label,
            "recorded_at_ms": recorded_at_ms,
            "files": sorted(source.name for source in existing),
        },
    )
    fsync_directory(account_dir)
    return destination


def _preserve_generation_files(
    *, account_dir: Path, recorded_at_ms: int, generation: int
) -> Path:
    names = (DB_FILENAME, f"{DB_FILENAME}-wal", f"{DB_FILENAME}-shm", MIRROR_FILENAME)
    existing = _preflight_regular_files(account_dir, names, label="generation")
    destination = (
        account_dir
        / PRESERVED_DIRECTORY
        / f"generation-{generation}-{recorded_at_ms}-{secrets.token_hex(4)}"
    )
    destination.mkdir(parents=True, exist_ok=False)
    fsync_directory_chain(destination, account_dir)
    moved: list[str] = []
    for source in existing:
        os.replace(source, destination / source.name)
        moved.append(source.name)
    manifest = {
        "schema_version": 1,
        "account_id": account_dir.name,
        "authority_generation": generation,
        "db_identity_token": _preserved_identity(destination),
        "recorded_at_ms": recorded_at_ms,
        "files": sorted(moved),
    }
    atomic_write_json(destination / "manifest.json", manifest)
    fsync_directory(account_dir)
    return destination


def _preserved_identity(destination: Path) -> str:
    db_path = destination / DB_FILENAME
    if not db_path.is_file():
        return "unavailable"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        row = conn.execute("SELECT db_identity_token FROM control_meta WHERE id = 1").fetchone()
        return str(row[0]) if row is not None else "unavailable"
    except sqlite3.DatabaseError:
        return "unavailable"
    finally:
        if conn is not None:
            conn.close()


def _restore_preserved_database(*, account_dir: Path, preserved: Path | None) -> None:
    if preserved is None:
        return
    for name in (DB_FILENAME, f"{DB_FILENAME}-wal", f"{DB_FILENAME}-shm"):
        source = preserved / name
        destination = account_dir / name
        if source.is_file() and not destination.exists():
            os.replace(source, destination)
    fsync_directory(account_dir)


def _preflight_regular_files(
    account_dir: Path, names: Sequence[str], *, label: str
) -> list[Path]:
    existing: list[Path] = []
    for name in names:
        source = account_dir / name
        if source.is_symlink():
            raise RecoveryRefused(
                f"refusing to preserve symbolic-link {label} artifact {source}"
            )
        if not source.exists():
            continue
        if not source.is_file():
            raise RecoveryRefused(
                f"refusing to preserve non-regular {label} artifact {source}"
            )
        existing.append(source)
    return existing


def _initialize_reset_generation(
    *,
    account_id: str,
    account_dir: Path,
    authority_generation: int,
    db_identity_token: str,
    reset_provenance: dict[str, Any],
    now_ms: int,
) -> None:
    assert_wal_filesystem_supported(account_dir)
    db_path = account_dir / DB_FILENAME
    mirror_path = account_dir / MIRROR_FILENAME
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        schema.configure_connection(conn)
        schema.apply_schema(conn)
        conn.execute(
            "INSERT INTO control_meta (id, schema_version, broker, account_id, "
            "db_identity_token, authority_generation, control_revision, created_at_ms, "
            "last_open_at_ms, reset_provenance_json, execution_lease_owner, "
            "execution_lease_expires_at_ms) VALUES "
            "(1, ?, 'alpaca', ?, ?, ?, 0, ?, ?, ?, NULL, NULL)",
            (
                schema.SCHEMA_VERSION,
                account_id,
                db_identity_token,
                authority_generation,
                now_ms,
                now_ms,
                canonical_json_bytes(reset_provenance).decode().strip(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    _fsync_file(db_path)
    mirror = MirrorFile(
        mirror_path,
        expected_identity=MirrorIdentity(
            account_id=account_id,
            authority_generation=authority_generation,
            db_identity_token=db_identity_token,
        ),
    )
    mirror.initialize_identity()


def _validate_reset_proof(
    *,
    account_id: str,
    proof: ResetBrokerProof,
    now_ms: int,
    max_proof_age_ms: int,
    stopped_strategy_instance_ids: Sequence[str],
    expected_strategy_instance_ids: Sequence[str],
) -> None:
    if type(proof.observed_at_ms) is not int or not isinstance(proof.proof_reference, str):
        raise RecoveryRefused("reset broker proof fields have invalid types")
    if not proof.proof_reference:
        raise RecoveryRefused("reset broker proof reference is required")
    if type(max_proof_age_ms) is not int:
        raise RecoveryRefused("reset proof freshness policy must be an integer")
    if proof.account_id != account_id:
        raise RecoveryRefused("reset broker proof belongs to a different account")
    if max_proof_age_ms < 1 or proof.observed_at_ms > now_ms:
        raise RecoveryRefused("reset broker proof timestamp or freshness policy is invalid")
    if now_ms - proof.observed_at_ms > max_proof_age_ms:
        raise RecoveryRefused("reset broker proof is stale")
    from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero

    if any(
        not isinstance(symbol, str)
        or not symbol
        or isinstance(quantity, bool)
        or not isinstance(quantity, (int, float))
        for symbol, quantity in proof.positions.items()
    ):
        raise RecoveryRefused("reset broker positions have invalid types")
    if any(not isinstance(order_id, str) or not order_id for order_id in proof.open_order_ids):
        raise RecoveryRefused("reset open-order identities have invalid types")
    quantities = tuple(float(quantity) for quantity in proof.positions.values())
    if any(not math.isfinite(quantity) for quantity in quantities):
        raise RecoveryRefused("reset broker position quantity must be finite")
    if any(position_quantity_is_nonzero(quantity) for quantity in quantities):
        raise RecoveryRefused("reset requires a broker-flat account")
    if proof.open_order_ids:
        raise RecoveryRefused("reset requires no open broker orders")
    if set(stopped_strategy_instance_ids) != set(expected_strategy_instance_ids):
        raise RecoveryRefused("reset requires every governed bot to be stopped")


def _write_recovery_receipt(account_dir: Path, receipt: RecoveryReceipt) -> Path:
    path = (
        account_dir
        / RECEIPT_DIRECTORY
        / f"{receipt.recorded_at_ms}-{receipt.operation.lower()}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fsync_directory_chain(path.parent, account_dir)
    atomic_write_json(path, asdict(receipt))
    return path


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
