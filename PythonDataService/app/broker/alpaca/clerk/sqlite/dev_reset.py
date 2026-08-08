"""Paper-only developer clean-slate reset for disposable Clerk authority."""

from __future__ import annotations

import os
import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from app.broker.alpaca.clerk.journal import INBOX_FILENAME, JOURNAL_FILENAME
from app.broker.alpaca.clerk.sqlite.developer_reset_registry import (
    DeveloperCleanSlateReset,
    DeveloperCleanSlateResetRegistry,
)
from app.broker.alpaca.clerk.sqlite.operational_files import (
    atomic_write_json,
    relative_reference,
    tree_sha256,
)
from app.broker.alpaca.clerk.sqlite.registry import EstablishedAccountsRegistry
from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME, MIRROR_FILENAME
from app.broker.alpaca.clerk.sqlite.repository_lifecycle import (
    assert_wal_filesystem_supported,
    exclusive_recovery_fence,
)
from app.broker.alpaca.clerk.sqlite.writes import account_paths
from app.broker.alpaca.paths import fsync_directory, fsync_directory_chain, safe_path_component
from app.engine.live.account_binding_ledger import (
    BINDING_COMMAND_LEDGER_FILENAME,
    read_account_binding_commands,
)
from app.engine.live.account_registry import (
    ACCOUNT_INSTANCE_REGISTRY_FILENAME,
    read_account_instance_registry,
)
from app.utils.timestamps import Clock, now_ms_utc

DEV_RESET_QUARANTINE_DIRECTORY = "dev-reset-quarantine"
MANIFEST_FILENAME = "manifest.json"
RECEIPT_FILENAME = "receipt.json"
_SQLITE_ARTIFACT_NAMES = (
    DB_FILENAME,
    f"{DB_FILENAME}-wal",
    f"{DB_FILENAME}-shm",
    MIRROR_FILENAME,
)
_LEGACY_ARTIFACT_NAMES = (
    INBOX_FILENAME,
    JOURNAL_FILENAME,
    "custody_resolution_receipts.json",
    "bots",
)
_RUNNER_ACCOUNT_ARTIFACT_NAMES = (
    ACCOUNT_INSTANCE_REGISTRY_FILENAME,
    BINDING_COMMAND_LEDGER_FILENAME,
    "bots",
)


class DeveloperCleanSlateResetRefused(Exception):
    """The paper-only reset cannot safely move the requested authority."""


@dataclass(frozen=True)
class DevResetArtifact:
    """One exact authority artifact moved into the reset quarantine."""

    source_scope: Literal["clerk", "runner"]
    source_reference: str
    quarantine_reference: str
    kind: Literal["file", "directory"]
    sha256: str


@dataclass(frozen=True)
class DevResetReceipt:
    """Durable result of one developer clean-slate reset."""

    operation: str
    account_id: str
    account_mode: str
    recorded_at_ms: int
    moved_artifacts: tuple[DevResetArtifact, ...]
    quarantine_reference: str | None
    manifest_reference: str | None
    manifest_sha256: str | None


@dataclass(frozen=True)
class _PlannedArtifact:
    source_scope: Literal["clerk", "runner"]
    source_root: Path
    source: Path
    destination_relative: Path
    kind: Literal["file", "directory"]
    sha256: str


def developer_clean_slate_reset(
    *,
    account_id: str,
    artifacts_root: Path,
    runner_artifacts_root: Path,
    account_mode: str,
    clock: Clock = now_ms_utc,
) -> DevResetReceipt:
    """Move paper authority aside without broker contact, import, or deletion."""
    if account_mode != "paper":
        raise DeveloperCleanSlateResetRefused(
            "developer clean-slate reset is available only for paper accounts"
        )
    try:
        with exclusive_recovery_fence(
            artifacts_root=artifacts_root,
            account_id=account_id,
        ):
            return _reset_fenced(
                account_id=account_id,
                artifacts_root=artifacts_root,
                runner_artifacts_root=runner_artifacts_root,
                account_mode=account_mode,
                clock=clock,
            )
    except DeveloperCleanSlateResetRefused:
        raise
    except Exception as exc:
        from app.broker.alpaca.clerk.sqlite.repository import RecoveryInProgress

        if isinstance(exc, RecoveryInProgress):
            raise DeveloperCleanSlateResetRefused(
                f"account {account_id!r} startup or recovery is already in progress"
            ) from exc
        raise


def _reset_fenced(
    *,
    account_id: str,
    artifacts_root: Path,
    runner_artifacts_root: Path,
    account_mode: str,
    clock: Clock,
) -> DevResetReceipt:
    accounts_root, account_dir = account_paths(artifacts_root, account_id)
    _require_directory_or_absent(account_dir, "Clerk account directory")
    runner_account_dir = _runner_account_dir(runner_artifacts_root, account_id)
    _require_directory_or_absent(runner_account_dir, "runner account directory")
    now = clock()
    planned = _collect_artifacts(
        account_dir=account_dir,
        runner_artifacts_root=runner_artifacts_root,
        runner_account_dir=runner_account_dir,
        account_id=account_id,
    )
    if not planned:
        return DevResetReceipt(
            operation="DEV_CLEAN_SLATE_RESET",
            account_id=account_id,
            account_mode=account_mode,
            recorded_at_ms=now,
            moved_artifacts=(),
            quarantine_reference=None,
            manifest_reference=None,
            manifest_sha256=None,
        )

    _assert_stopped_sqlite_authority(account_dir, now_ms=now)
    assert_wal_filesystem_supported(account_dir)
    established = EstablishedAccountsRegistry(accounts_root).latest(account_id)
    quarantine_root = account_dir / DEV_RESET_QUARANTINE_DIRECTORY
    _require_directory_or_absent(quarantine_root, "developer reset quarantine root")
    quarantine_dir = quarantine_root / f"reset-{now}-{secrets.token_hex(4)}"
    if quarantine_dir.exists() or quarantine_dir.is_symlink():
        raise DeveloperCleanSlateResetRefused("developer reset quarantine destination exists")

    moved: list[_PlannedArtifact] = []
    try:
        quarantine_dir.mkdir(parents=True, exist_ok=False)
        fsync_directory_chain(quarantine_dir, artifacts_root)
        for artifact in planned:
            destination = quarantine_dir / artifact.destination_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            _require_safe_destination(destination)
            os.replace(artifact.source, destination)
            moved.append(artifact)
            fsync_directory(artifact.source.parent)
            fsync_directory(destination.parent)

        receipt_artifacts = tuple(_to_receipt_artifact(artifact) for artifact in planned)
        manifest_path = quarantine_dir / MANIFEST_FILENAME
        manifest_sha256 = atomic_write_json(
            manifest_path,
            {
                "schema_version": 1,
                "operation": "DEV_CLEAN_SLATE_RESET",
                "account_id": account_id,
                "account_mode": account_mode,
                "recorded_at_ms": now,
                "prior_authority_generation": (
                    established.authority_generation if established is not None else None
                ),
                "intentional_omissions": [
                    "broker_flat_proof",
                    "runner_roll_call",
                    "confirmation_token",
                ],
                "moved_artifacts": [asdict(artifact) for artifact in receipt_artifacts],
            },
        )
        receipt = DevResetReceipt(
            operation="DEV_CLEAN_SLATE_RESET",
            account_id=account_id,
            account_mode=account_mode,
            recorded_at_ms=now,
            moved_artifacts=receipt_artifacts,
            quarantine_reference=relative_reference(artifacts_root, quarantine_dir),
            manifest_reference=relative_reference(artifacts_root, manifest_path),
            manifest_sha256=manifest_sha256,
        )
        atomic_write_json(quarantine_dir / RECEIPT_FILENAME, asdict(receipt))
        if established is not None:
            DeveloperCleanSlateResetRegistry(accounts_root).record(
                DeveloperCleanSlateReset(
                    account_id=account_id,
                    prior_authority_generation=established.authority_generation,
                    recorded_at_ms=now,
                    manifest_reference=receipt.manifest_reference,
                    manifest_sha256=manifest_sha256,
                )
            )
        return receipt
    except Exception:
        _rollback_moves(moved, quarantine_dir)
        _remove_empty_quarantine(quarantine_dir)
        raise


def _collect_artifacts(
    *,
    account_dir: Path,
    runner_artifacts_root: Path,
    runner_account_dir: Path,
    account_id: str,
) -> tuple[_PlannedArtifact, ...]:
    planned = [
        *_plan_named_artifacts(
            source_scope="clerk",
            source_root=account_dir,
            names=_SQLITE_ARTIFACT_NAMES,
            destination_prefix=Path("sqlite-authority"),
            allowed_directories=frozenset(),
        ),
        *_plan_named_artifacts(
            source_scope="clerk",
            source_root=account_dir,
            names=_LEGACY_ARTIFACT_NAMES,
            destination_prefix=Path("legacy-authority"),
            allowed_directories=frozenset({"bots"}),
        ),
        *_plan_named_artifacts(
            source_scope="runner",
            source_root=runner_account_dir,
            names=_RUNNER_ACCOUNT_ARTIFACT_NAMES,
            destination_prefix=Path("runner-account"),
            allowed_directories=frozenset({"bots"}),
        ),
    ]
    for strategy_instance_id in _runner_strategy_instance_ids(
        runner_artifacts_root,
        account_id,
    ):
        source = runner_artifacts_root / "live_state" / strategy_instance_id
        if not source.exists() and not source.is_symlink():
            continue
        planned.append(
            _plan_artifact(
                source_scope="runner",
                source_root=runner_artifacts_root,
                source=source,
                destination_relative=Path("runner-catalogs") / strategy_instance_id,
                allow_directory=True,
            )
        )
    destinations = [artifact.destination_relative for artifact in planned]
    if len(destinations) != len(set(destinations)):
        raise DeveloperCleanSlateResetRefused("developer reset artifact plan overlaps")
    return tuple(planned)


def _plan_named_artifacts(
    *,
    source_scope: Literal["clerk", "runner"],
    source_root: Path,
    names: Sequence[str],
    destination_prefix: Path,
    allowed_directories: frozenset[str],
) -> tuple[_PlannedArtifact, ...]:
    planned: list[_PlannedArtifact] = []
    for name in names:
        source = source_root / name
        if not source.exists() and not source.is_symlink():
            continue
        planned.append(
            _plan_artifact(
                source_scope=source_scope,
                source_root=source_root,
                source=source,
                destination_relative=destination_prefix / name,
                allow_directory=name in allowed_directories,
            )
        )
    return tuple(planned)


def _plan_artifact(
    *,
    source_scope: Literal["clerk", "runner"],
    source_root: Path,
    source: Path,
    destination_relative: Path,
    allow_directory: bool,
) -> _PlannedArtifact:
    if source.is_symlink():
        raise DeveloperCleanSlateResetRefused(
            f"developer reset refuses symbolic-link authority artifact {source}"
        )
    if source.is_file():
        kind: Literal["file", "directory"] = "file"
    elif allow_directory and source.is_dir():
        kind = "directory"
    else:
        raise DeveloperCleanSlateResetRefused(
            f"developer reset refuses non-regular authority artifact {source}"
        )
    try:
        sha256 = tree_sha256(source)
        source_reference = source.relative_to(source_root).as_posix()
    except ValueError as exc:
        raise DeveloperCleanSlateResetRefused(
            f"developer reset cannot safely inspect authority artifact {source}: {exc}"
        ) from exc
    if not source_reference:
        raise DeveloperCleanSlateResetRefused("developer reset artifact has no relative path")
    return _PlannedArtifact(
        source_scope=source_scope,
        source_root=source_root,
        source=source,
        destination_relative=destination_relative,
        kind=kind,
        sha256=sha256,
    )


def _runner_strategy_instance_ids(
    runner_artifacts_root: Path,
    account_id: str,
) -> tuple[str, ...]:
    try:
        registry_ids = {
            binding.strategy_instance_id
            for binding in read_account_instance_registry(runner_artifacts_root, account_id)
        }
        ledger_ids = {
            command.strategy_instance_id
            for command in read_account_binding_commands(runner_artifacts_root, account_id)
        }
    except (OSError, ValueError) as exc:
        raise DeveloperCleanSlateResetRefused(
            f"runner catalog authority for {account_id!r} is unreadable: {exc}"
        ) from exc
    return tuple(sorted(registry_ids | ledger_ids))


def _assert_stopped_sqlite_authority(account_dir: Path, *, now_ms: int) -> None:
    db_path = account_dir / DB_FILENAME
    sidecars = tuple(account_dir / f"{DB_FILENAME}{suffix}" for suffix in ("-wal", "-shm"))
    if not db_path.exists() and not db_path.is_symlink():
        if any(path.exists() or path.is_symlink() for path in sidecars):
            raise DeveloperCleanSlateResetRefused(
                "SQLite sidecars exist without a database; authority stop state is unsafe"
            )
        return
    _require_regular_file(db_path, "SQLite database")
    for sidecar in sidecars:
        if not sidecar.exists() and not sidecar.is_symlink():
            continue
        _require_regular_file(sidecar, "SQLite sidecar")
        if sidecar.stat().st_size != 0:
            raise DeveloperCleanSlateResetRefused(
                "developer reset requires a cleanly stopped SQLite authority; "
                f"found non-empty {sidecar.name}"
            )
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT execution_lease_owner, execution_lease_expires_at_ms "
            "FROM control_meta WHERE id = 1"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise DeveloperCleanSlateResetRefused(
            "SQLite execution lease cannot be verified; authority must remain in place"
        ) from exc
    finally:
        if conn is not None:
            conn.close()
    if row is None or (row[0] is None) != (row[1] is None):
        raise DeveloperCleanSlateResetRefused("SQLite execution lease state is invalid")
    if row[0] is not None and row[1] >= now_ms:
        raise DeveloperCleanSlateResetRefused(
            "developer reset requires the active Clerk process to be stopped"
        )


def _runner_account_dir(runner_artifacts_root: Path, account_id: str) -> Path:
    safe_account_id = safe_path_component(account_id, "account_id")
    return runner_artifacts_root / "accounts" / safe_account_id


def _require_directory_or_absent(path: Path, label: str) -> None:
    if path.is_symlink():
        raise DeveloperCleanSlateResetRefused(f"{label} must not be a symbolic link")
    if path.exists() and not path.is_dir():
        raise DeveloperCleanSlateResetRefused(f"{label} must be a regular directory")


def _require_regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise DeveloperCleanSlateResetRefused(f"{label} must be a regular non-symbolic-link file")


def _require_safe_destination(path: Path) -> None:
    if path.is_symlink() or path.exists():
        raise DeveloperCleanSlateResetRefused(
            f"developer reset quarantine destination is unsafe: {path}"
        )


def _to_receipt_artifact(artifact: _PlannedArtifact) -> DevResetArtifact:
    return DevResetArtifact(
        source_scope=artifact.source_scope,
        source_reference=artifact.source.relative_to(artifact.source_root).as_posix(),
        quarantine_reference=artifact.destination_relative.as_posix(),
        kind=artifact.kind,
        sha256=artifact.sha256,
    )


def _rollback_moves(moved: Sequence[_PlannedArtifact], quarantine_dir: Path) -> None:
    for artifact in reversed(moved):
        destination = quarantine_dir / artifact.destination_relative
        if destination.exists() and not artifact.source.exists():
            artifact.source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, artifact.source)
            fsync_directory(destination.parent)
            fsync_directory(artifact.source.parent)


def _remove_empty_quarantine(quarantine_dir: Path) -> None:
    for path in (quarantine_dir / RECEIPT_FILENAME, quarantine_dir / MANIFEST_FILENAME):
        path.unlink(missing_ok=True)
    for directory in sorted(
        (path for path in quarantine_dir.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        directory.rmdir()
    quarantine_dir.rmdir()
    fsync_directory(quarantine_dir.parent)
