"""Protected storage proof and recovery ceremony for synthetic qualification."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.broker.alpaca.clerk.sqlite import writes
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.database_verification import (
    DatabaseVerificationFailed,
    verify_database,
)
from app.broker.alpaca.clerk.sqlite.mirror import MirrorChainBroken
from app.broker.alpaca.clerk.sqlite.recovery import (
    RecoveryRefusalReason,
    RecoveryRefused,
    create_verified_backup,
    preserve_and_rebuild_from_mirror,
    restore_verified_backup,
    verify_authority_head,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.repository_lifecycle import (
    assert_wal_filesystem_supported,
    wal_filesystem_type,
)
from app.schemas.account_custody_qualification import (
    SyntheticProtectedGenerationEvidence,
    SyntheticRecoveryEvidence,
)

QUALIFICATION_ARTIFACTS_ROOT = Path("/app/artifacts/alpaca_clerk/qualification")
PRODUCTION_ARTIFACTS_ROOT = Path("/app/production-alpaca-clerk")


class SyntheticRecoveryCustodyError(RuntimeError):
    """Recovery integrity or hash-head evidence disagreed after execution."""


@dataclass(frozen=True)
class ProtectedGenerationRun:
    """Protected authority and recovery evidence consumed by report reduction."""

    evidence: SyntheticProtectedGenerationEvidence
    recovery: SyntheticRecoveryEvidence
    command_ids: tuple[str, ...]
    receipt_ids: tuple[str, ...]
    control_revision: int
    recovery_source_event_at_ms: int
    recovery_clerk_observed_at_ms: int
    recovery_recorded_at_ms: int


@dataclass(frozen=True)
class _MountRecord:
    mount_id: int
    mount_point: Path
    options: frozenset[str]


@dataclass(frozen=True)
class QualificationStorageBoundary:
    """Observed one-shot mount topology used in the protection receipt."""

    filesystem_type: Literal["xfs"]
    qualification_mount_id: int
    production_mount_id: int
    qualification_mount_access: Literal["READ_WRITE"]
    production_mount_access: Literal["READ_ONLY"]
    mount_identity_separated: Literal[True]
    production_authority_root: Path


def run_protected_generation(
    *,
    artifacts_root: Path,
    production_artifacts_root: Path,
    production_account_id: str,
    generated_at_ms: int,
    storage_boundary_resolver: Callable[[Path, Path], QualificationStorageBoundary] | None = None,
) -> ProtectedGenerationRun:
    """Create an isolated authority and perform its complete recovery ceremony."""

    account_id = f"QUAL-1415-{generated_at_ms}"
    if account_id == production_account_id:
        raise ValueError("qualification authority must differ from the production account")
    storage_boundary = (storage_boundary_resolver or assert_qualification_storage_boundary)(
        artifacts_root,
        production_artifacts_root,
    )
    _accounts_root, account_directory = writes.account_paths(
        artifacts_root,
        account_id,
    )
    clock_value = generated_at_ms

    def clock() -> int:
        nonlocal clock_value
        observed = clock_value
        clock_value += 1
        return observed

    repo = ClerkSqliteRepository.initialize(
        account_id=account_id,
        artifacts_root=artifacts_root,
        clock=clock,
    )
    repo.register_strategy_instance(
        strategy_instance_id="spy-qualification",
        symbol="SPY",
        config_hash="issue-1415",
    )
    start = submit_start_run(
        repo,
        account_id=account_id,
        strategy_instance_id="spy-qualification",
        lifecycle_run_id="synthetic-rehearsal",
        operator_reason="issue-1415 protected-generation rehearsal",
        clock=clock,
    )
    stop = submit_stop_run(
        repo,
        account_id=account_id,
        strategy_instance_id="spy-qualification",
        lifecycle_run_id="synthetic-rehearsal",
        operator_reason="issue-1415 recovery ceremony",
        clock=clock,
    )
    meta = repo.control_meta_snapshot()
    repo.close()

    try:
        before = verify_authority_head(
            account_id=account_id,
            artifacts_root=artifacts_root,
        )
        backup = create_verified_backup(
            account_id=account_id,
            artifacts_root=artifacts_root,
            clock=clock,
        )
        backup_manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
        backup_created_at_ms = backup_manifest.get("created_at_ms")
        if type(backup_created_at_ms) is not int:
            raise RuntimeError("verified backup manifest has no integer creation clock")
        restore = restore_verified_backup(
            account_id=account_id,
            artifacts_root=artifacts_root,
            bundle_path=backup.bundle_path,
            clock=clock,
        )
        after_restore = verify_authority_head(
            account_id=account_id,
            artifacts_root=artifacts_root,
        )
        rebuild = preserve_and_rebuild_from_mirror(
            account_id=account_id,
            artifacts_root=artifacts_root,
            clock=clock,
        )
        after_rebuild = verify_authority_head(
            account_id=account_id,
            artifacts_root=artifacts_root,
        )
        offline = verify_database(
            account_directory / "clerk.db",
            expected_account_id=account_id,
            expected_generation=meta.authority_generation,
            expected_db_identity=meta.db_identity_token,
        )
    except (DatabaseVerificationFailed, MirrorChainBroken) as exc:
        raise SyntheticRecoveryCustodyError(f"recovery integrity verification failed: {exc}") from exc
    except RecoveryRefused as exc:
        if exc.reason is RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT:
            raise SyntheticRecoveryCustodyError(f"recovery integrity verification was refused: {exc}") from exc
        raise
    observed_heads = {
        before.last_row_hash,
        after_restore.last_row_hash,
        after_rebuild.last_row_hash,
        offline.last_row_hash,
    }
    if len(observed_heads) != 1:
        raise SyntheticRecoveryCustodyError("backup, restore, rebuild, and offline integrity hash heads disagree")
    command_ids = (start.command.command_id, stop.command.command_id)
    receipt_ids = tuple(value for value in (start.command.receipt_id, stop.command.receipt_id) if value is not None)
    return ProtectedGenerationRun(
        evidence=SyntheticProtectedGenerationEvidence(
            artifacts_root=str(artifacts_root),
            account_id=account_id,
            authority_generation=meta.authority_generation,
            db_identity_token=meta.db_identity_token,
            filesystem_type=storage_boundary.filesystem_type,
            wal_storage_guard_checked=True,
            production_account_id=production_account_id,
            production_authority_root=str(storage_boundary.production_authority_root),
            production_mount_access=storage_boundary.production_mount_access,
            qualification_mount_access=(storage_boundary.qualification_mount_access),
            production_mount_id=storage_boundary.production_mount_id,
            qualification_mount_id=storage_boundary.qualification_mount_id,
            mount_identity_separated=storage_boundary.mount_identity_separated,
            production_generation_opened_read_write=(storage_boundary.production_mount_access != "READ_ONLY"),
            diagnostic_read_boundary="ONE_SHOT_SERVICE_CONTAINER",
        ),
        recovery=SyntheticRecoveryEvidence(
            backup_manifest_ref=str(backup.manifest_path),
            backup_sha256=backup.snapshot_sha256,
            restore_receipt_ref=f"{restore.operation}:{restore.recorded_at_ms}",
            rebuild_receipt_ref=f"{rebuild.operation}:{rebuild.recorded_at_ms}",
            hash_head_before=before.last_row_hash,
            hash_head_after_restore=after_restore.last_row_hash,
            hash_head_after_rebuild=after_rebuild.last_row_hash,
        ),
        command_ids=command_ids,
        receipt_ids=receipt_ids or (f"hash-head:{before.last_row_hash}",),
        control_revision=meta.control_revision,
        recovery_source_event_at_ms=backup_created_at_ms,
        recovery_clerk_observed_at_ms=restore.recorded_at_ms,
        recovery_recorded_at_ms=rebuild.recorded_at_ms,
    )


def assert_qualification_storage_boundary(
    artifacts_root: Path,
    production_artifacts_root: Path,
) -> QualificationStorageBoundary:
    """Prove the one-shot named-volume boundary before opening SQLite."""

    try:
        resolved_root = artifacts_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("qualification artifacts root must already exist") from exc
    if resolved_root != QUALIFICATION_ARTIFACTS_ROOT:
        raise RuntimeError("qualification authority must use the protected one-shot named-volume root")
    try:
        resolved_production_root = production_artifacts_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("production authority root must already exist") from exc
    if resolved_production_root != PRODUCTION_ARTIFACTS_ROOT:
        raise RuntimeError("production authority must use the protected read-only one-shot mount")
    if resolved_root == resolved_production_root:
        raise RuntimeError("qualification and production authority roots must differ")
    assert_wal_filesystem_supported(resolved_root)
    filesystem_type = wal_filesystem_type(resolved_root)
    if filesystem_type != "xfs":
        raise RuntimeError("qualification authority requires the XFS named volume")
    return prove_mount_topology(
        qualification_root=resolved_root,
        production_root=resolved_production_root,
        mountinfo=Path("/proc/self/mountinfo").read_text(encoding="utf-8"),
    )


def prove_mount_topology(
    *,
    qualification_root: Path,
    production_root: Path,
    mountinfo: str,
) -> QualificationStorageBoundary:
    """Derive read/write protection from the process's actual mount table."""

    records = _parse_mountinfo(mountinfo)
    qualification_mount = _mount_for_path(qualification_root, records)
    production_mount = _mount_for_path(production_root, records)
    if qualification_mount.mount_id == production_mount.mount_id:
        raise RuntimeError("qualification and production authority must use distinct mounts")
    if "rw" not in qualification_mount.options:
        raise RuntimeError("qualification authority mount must be read-write")
    if "ro" not in production_mount.options or "rw" in production_mount.options:
        raise RuntimeError("production authority mount must be read-only")
    return QualificationStorageBoundary(
        filesystem_type="xfs",
        qualification_mount_id=qualification_mount.mount_id,
        production_mount_id=production_mount.mount_id,
        qualification_mount_access="READ_WRITE",
        production_mount_access="READ_ONLY",
        mount_identity_separated=True,
        production_authority_root=production_root,
    )


def _parse_mountinfo(mountinfo: str) -> tuple[_MountRecord, ...]:
    records: list[_MountRecord] = []
    for line in mountinfo.splitlines():
        fields = line.split()
        separator = fields.index("-") if "-" in fields else -1
        if separator < 6:
            raise RuntimeError("mountinfo contains an invalid record")
        records.append(
            _MountRecord(
                mount_id=int(fields[0]),
                mount_point=Path(_unescape_mountinfo_path(fields[4])),
                options=frozenset(fields[5].split(",")),
            )
        )
    if not records:
        raise RuntimeError("mountinfo did not contain any mounts")
    return tuple(records)


def _mount_for_path(
    path: Path,
    records: tuple[_MountRecord, ...],
) -> _MountRecord:
    candidates = tuple(record for record in records if path == record.mount_point or record.mount_point in path.parents)
    if not candidates:
        raise RuntimeError(f"no mountinfo record covers {path}")
    return max(candidates, key=lambda record: len(record.mount_point.parts))


def _unescape_mountinfo_path(value: str) -> str:
    return value.replace(r"\040", " ").replace(r"\011", "\t").replace(r"\012", "\n").replace(r"\134", "\\")


__all__ = (
    "PRODUCTION_ARTIFACTS_ROOT",
    "QUALIFICATION_ARTIFACTS_ROOT",
    "ProtectedGenerationRun",
    "QualificationStorageBoundary",
    "SyntheticRecoveryCustodyError",
    "assert_qualification_storage_boundary",
    "prove_mount_topology",
    "run_protected_generation",
)
