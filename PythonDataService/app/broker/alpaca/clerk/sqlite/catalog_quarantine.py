"""Bounded quarantine for disposable pre-SQLite Alpaca runner catalogs.

The activated SQLite strategy registry decides which Alpaca runner directories
remain current.  This offline plan/apply ceremony moves only file-backed
Alpaca bindings absent from that registry.  It never deletes broker evidence
or touches directories owned by another broker.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import ValidationError

from app.broker.alpaca.clerk.sqlite import writes
from app.broker.alpaca.clerk.sqlite.activation import ActivationStore
from app.broker.alpaca.clerk.sqlite.database_verification import (
    DatabaseVerification,
    verify_database,
)
from app.broker.alpaca.clerk.sqlite.operational_files import (
    atomic_write_json,
    canonical_json_bytes,
    confined_relative_path,
    relative_reference,
    tree_sha256,
)
from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME
from app.broker.alpaca.paths import fsync_directory, fsync_directory_chain
from app.engine.live.account_artifacts import AccountArtifactError
from app.engine.live.account_registry import (
    index_account_instance_bindings,
    read_account_instance_registry,
)
from app.services.bot_binding_repository import (
    BINDING_FILENAME,
    STRATEGY_INSTANCE_FILENAME,
    BotBindingRepository,
    StrategyInstanceRecord,
)
from app.utils.timestamps import now_ms_utc

QUARANTINE_DIRECTORY = "legacy-catalog-quarantine"
MANIFEST_FILENAME = "manifest.json"
RECEIPT_FILENAME = "receipt.json"


class CatalogQuarantineRefused(Exception):
    """The cleanup proof is unsafe, stale, changed, or too broad."""


@dataclass(frozen=True)
class CatalogArtifactEvidence:
    strategy_instance_id: str
    relative_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class CatalogQuarantinePlan:
    schema_version: int
    plan_id: str
    confirmation_token: str
    account_id: str
    created_at_ms: int
    expires_at_ms: int
    max_candidates: int
    max_total_bytes: int
    database: DatabaseVerification
    registered_strategy_instance_ids: tuple[str, ...]
    candidates: tuple[CatalogArtifactEvidence, ...]


@dataclass(frozen=True)
class CatalogQuarantineReceipt:
    operation: str
    plan_id: str
    account_id: str
    authority_generation: int
    db_identity_token: str
    quarantined_count: int
    quarantined_bytes: int
    quarantine_reference: str
    manifest_sha256: str
    recorded_at_ms: int


@dataclass(frozen=True)
class _ActiveRegistry:
    database: DatabaseVerification
    strategy_instance_ids: tuple[str, ...]


def plan_catalog_quarantine(
    *,
    artifacts_root: Path,
    runner_artifacts_root: Path,
    account_id: str,
    max_candidates: int,
    max_total_bytes: int,
    confirmation_ttl_ms: int = 120_000,
) -> CatalogQuarantinePlan:
    """Inventory the exact disposable catalog set and mint a short-lived token."""
    _validate_limits(max_candidates, max_total_bytes, confirmation_ttl_ms)
    active = _read_active_registry(artifacts_root, account_id)
    account_strategy_instance_ids = _read_account_strategy_instance_ids(
        runner_artifacts_root,
        account_id,
    )
    candidates = _catalog_candidates(
        runner_artifacts_root,
        account_strategy_instance_ids=account_strategy_instance_ids,
        registered_ids=frozenset(active.strategy_instance_ids),
        max_candidates=max_candidates,
        max_total_bytes=max_total_bytes,
    )
    if not candidates:
        raise CatalogQuarantineRefused("no disposable Alpaca catalog bindings were found")
    created_at_ms = now_ms_utc()
    payload = _plan_payload(
        account_id=account_id,
        created_at_ms=created_at_ms,
        expires_at_ms=created_at_ms + confirmation_ttl_ms,
        max_candidates=max_candidates,
        max_total_bytes=max_total_bytes,
        database=active.database,
        registered_strategy_instance_ids=active.strategy_instance_ids,
        candidates=candidates,
    )
    token = hashlib.sha256(canonical_json_bytes(_jsonable(payload))).hexdigest()
    return CatalogQuarantinePlan(
        schema_version=1,
        plan_id=token,
        confirmation_token=token,
        **payload,
    )


def apply_catalog_quarantine(
    *,
    plan: CatalogQuarantinePlan,
    confirmation_token: str,
    artifacts_root: Path,
    runner_artifacts_root: Path,
) -> CatalogQuarantineReceipt:
    """Recheck a plan, then move only its exact candidates into quarantine."""
    _validate_plan(plan, confirmation_token)
    quarantine_root = runner_artifacts_root / QUARANTINE_DIRECTORY
    quarantine_dir = quarantine_root / plan.plan_id
    prepared_manifest = quarantine_dir / MANIFEST_FILENAME
    receipt_path = quarantine_dir / RECEIPT_FILENAME
    _require_safe_quarantine_paths(
        runner_artifacts_root=runner_artifacts_root,
        quarantine_root=quarantine_root,
        quarantine_dir=quarantine_dir,
        prepared_manifest=prepared_manifest,
        receipt_path=receipt_path,
        candidates=plan.candidates,
    )
    resuming = prepared_manifest.is_file() and not receipt_path.exists()
    durable_recovery_required = resuming and _has_durably_moved_candidate(
        quarantine_dir,
        plan.candidates,
    )
    if now_ms_utc() > plan.expires_at_ms and not durable_recovery_required:
        raise CatalogQuarantineRefused("catalog quarantine plan expired")

    active = _read_active_registry(artifacts_root, plan.account_id)
    if active.database != plan.database:
        raise CatalogQuarantineRefused("SQLite authority changed after catalog quarantine planning")
    if active.strategy_instance_ids != plan.registered_strategy_instance_ids:
        raise CatalogQuarantineRefused("SQLite strategy registry changed after catalog quarantine planning")
    account_strategy_instance_ids = _read_account_strategy_instance_ids(
        runner_artifacts_root,
        plan.account_id,
    )
    current = _catalog_candidates(
        runner_artifacts_root,
        account_strategy_instance_ids=account_strategy_instance_ids,
        registered_ids=frozenset(active.strategy_instance_ids),
        max_candidates=plan.max_candidates,
        max_total_bytes=plan.max_total_bytes,
        quarantine_dir=quarantine_dir if resuming else None,
    )
    if current != plan.candidates:
        raise CatalogQuarantineRefused("legacy catalog evidence changed after planning")

    manifest_payload = {
        "schema_version": 1,
        "state": "prepared",
        "plan_id": plan.plan_id,
        "account_id": plan.account_id,
        "authority_generation": plan.database.authority_generation,
        "db_identity_token": plan.database.db_identity_token,
        "registered_strategy_instance_ids": list(plan.registered_strategy_instance_ids),
        "candidates": [asdict(item) for item in plan.candidates],
    }
    quarantine_dir.mkdir(parents=True, exist_ok=resuming)
    fsync_directory_chain(quarantine_dir, runner_artifacts_root)
    if resuming:
        _require_manifest(prepared_manifest, manifest_payload)
    else:
        if receipt_path.exists() or any(quarantine_dir.iterdir()):
            raise CatalogQuarantineRefused("catalog quarantine destination is not empty")
        atomic_write_json(prepared_manifest, manifest_payload)

    moved: list[tuple[Path, Path]] = []
    try:
        for artifact in plan.candidates:
            source = confined_relative_path(runner_artifacts_root, artifact.relative_path)
            destination = quarantine_dir / artifact.strategy_instance_id
            if destination.is_symlink():
                raise CatalogQuarantineRefused(
                    "catalog quarantine destination must not be a symbolic link"
                )
            if destination.is_dir() and not source.exists():
                if tree_sha256(destination) != artifact.sha256:
                    raise CatalogQuarantineRefused("resumed quarantine artifact hash mismatch")
                continue
            if source.is_symlink() or not source.is_dir() or destination.exists():
                raise CatalogQuarantineRefused("catalog quarantine source/destination state is unsafe")
            if tree_sha256(source) != artifact.sha256:
                raise CatalogQuarantineRefused("catalog artifact changed during quarantine")
            os.replace(source, destination)
            moved.append((source, destination))
            fsync_directory(source.parent)
            fsync_directory(destination.parent)
    except Exception:
        _rollback_moves(moved)
        if not _has_durably_moved_candidate(quarantine_dir, plan.candidates):
            _remove_prepared_manifest_after_rollback(
                prepared_manifest,
                expected=manifest_payload,
            )
        raise

    manifest_payload["state"] = "applied"
    manifest_sha256 = atomic_write_json(prepared_manifest, manifest_payload)
    recorded_at_ms = now_ms_utc()
    receipt = CatalogQuarantineReceipt(
        operation="catalog-quarantine",
        plan_id=plan.plan_id,
        account_id=plan.account_id,
        authority_generation=plan.database.authority_generation,
        db_identity_token=plan.database.db_identity_token,
        quarantined_count=len(plan.candidates),
        quarantined_bytes=sum(item.size_bytes for item in plan.candidates),
        quarantine_reference=relative_reference(runner_artifacts_root, quarantine_dir),
        manifest_sha256=manifest_sha256,
        recorded_at_ms=recorded_at_ms,
    )
    atomic_write_json(receipt_path, asdict(receipt))
    return receipt


def _read_active_registry(artifacts_root: Path, account_id: str) -> _ActiveRegistry:
    accounts_root, account_dir = writes.account_paths(artifacts_root, account_id)
    _require_stopped_authority(account_dir)
    activation = ActivationStore(accounts_root).latest(account_id)
    if activation is None:
        raise CatalogQuarantineRefused("catalog quarantine requires activated SQLite authority")
    database = verify_database(
        account_dir / DB_FILENAME,
        expected_account_id=account_id,
        expected_generation=activation.authority_generation,
        expected_db_identity=activation.db_identity_token,
    )
    resolved = ActivationStore(accounts_root).resolve(
        account_id,
        database.authority_generation,
        database.db_identity_token,
        artifacts_root,
    )
    if resolved is None:
        raise CatalogQuarantineRefused("SQLite activation fence is missing")
    db_path = (account_dir / DB_FILENAME).resolve(strict=True)
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT strategy_instance_id FROM strategy_instances ORDER BY strategy_instance_id"
        ).fetchall()
    finally:
        conn.close()
    return _ActiveRegistry(
        database=database,
        strategy_instance_ids=tuple(str(row[0]) for row in rows),
    )


def _read_account_strategy_instance_ids(
    runner_artifacts_root: Path,
    account_id: str,
) -> frozenset[str]:
    """Return only runner identities durably bound to the selected account."""
    try:
        bindings = read_account_instance_registry(runner_artifacts_root, account_id)
        indexed = index_account_instance_bindings(bindings, account_id=account_id)
    except (AccountArtifactError, OSError, ValidationError, ValueError) as exc:
        raise CatalogQuarantineRefused(
            f"runner account registry for {account_id!r} is unreadable: {exc}"
        ) from exc
    return frozenset(indexed.latest_by_instance)


def _catalog_candidates(
    runner_artifacts_root: Path,
    *,
    account_strategy_instance_ids: frozenset[str],
    registered_ids: frozenset[str],
    max_candidates: int,
    max_total_bytes: int,
    quarantine_dir: Path | None = None,
) -> tuple[CatalogArtifactEvidence, ...]:
    live_state_root = runner_artifacts_root / "live_state"
    if live_state_root.is_symlink() or not live_state_root.is_dir():
        raise CatalogQuarantineRefused("runner live_state root is missing or unsafe")
    evidence: list[CatalogArtifactEvidence] = []
    total_bytes = 0
    for instance_dir in sorted(live_state_root.iterdir(), key=lambda path: path.name):
        if instance_dir.is_symlink():
            raise CatalogQuarantineRefused("runner catalog contains a symbolic-link entry")
        if not instance_dir.is_dir():
            continue
        identity = _binding_identity(instance_dir)
        if (
            identity is None
            or identity[1] != "alpaca"
            or identity[0] not in account_strategy_instance_ids
            or identity[0] in registered_ids
        ):
            continue
        if identity[0] != instance_dir.name:
            raise CatalogQuarantineRefused("runner binding identity does not match its directory")
        size_bytes = _tree_size(instance_dir)
        total_bytes += size_bytes
        evidence.append(
            CatalogArtifactEvidence(
                strategy_instance_id=identity[0],
                relative_path=relative_reference(runner_artifacts_root, instance_dir),
                sha256=tree_sha256(instance_dir),
                size_bytes=size_bytes,
            )
        )
        if len(evidence) > max_candidates or total_bytes > max_total_bytes:
            raise CatalogQuarantineRefused("legacy catalog exceeds the operator-declared bounds")
    if quarantine_dir is not None:
        if quarantine_dir.is_symlink() or not quarantine_dir.is_dir():
            raise CatalogQuarantineRefused("quarantine directory is missing or unsafe")
        by_sid = {item.strategy_instance_id: item for item in evidence}
        for destination in sorted(quarantine_dir.iterdir(), key=lambda path: path.name):
            if destination.name in {MANIFEST_FILENAME, RECEIPT_FILENAME}:
                continue
            if destination.is_symlink() or not destination.is_dir():
                raise CatalogQuarantineRefused("quarantine contains an unsafe entry")
            if destination.name not in account_strategy_instance_ids:
                raise CatalogQuarantineRefused(
                    "quarantine contains an artifact outside the selected account"
                )
            size_bytes = _tree_size(destination)
            item = CatalogArtifactEvidence(
                strategy_instance_id=destination.name,
                relative_path=f"live_state/{destination.name}",
                sha256=tree_sha256(destination),
                size_bytes=size_bytes,
            )
            previous = by_sid.setdefault(destination.name, item)
            if previous != item:
                raise CatalogQuarantineRefused("quarantine and live catalog disagree")
        evidence = list(by_sid.values())
    return tuple(sorted(evidence, key=lambda item: item.strategy_instance_id))


def _binding_identity(instance_dir: Path) -> tuple[str, str] | None:
    normalized = instance_dir / STRATEGY_INSTANCE_FILENAME
    legacy = instance_dir / BINDING_FILENAME
    path = normalized if normalized.is_file() else legacy if legacy.is_file() else None
    if path is None:
        return None
    if path.is_symlink():
        raise CatalogQuarantineRefused("runner binding must not be a symbolic link")
    try:
        if path.name == STRATEGY_INSTANCE_FILENAME:
            binding = StrategyInstanceRecord.model_validate_json(path.read_text(encoding="utf-8"))
        else:
            binding = BotBindingRepository.decode(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise CatalogQuarantineRefused(f"runner binding {path} is unreadable: {exc}") from exc
    return binding.strategy_instance_id, binding.broker


def _tree_size(path: Path) -> int:
    total = 0
    for candidate in path.rglob("*"):
        if candidate.is_symlink():
            raise CatalogQuarantineRefused("catalog artifact tree contains a symbolic link")
        if candidate.is_file():
            total += candidate.stat().st_size
        elif not candidate.is_dir():
            raise CatalogQuarantineRefused("catalog artifact tree contains an unsupported entry")
    return total


def _plan_payload(
    *,
    account_id: str,
    created_at_ms: int,
    expires_at_ms: int,
    max_candidates: int,
    max_total_bytes: int,
    database: DatabaseVerification,
    registered_strategy_instance_ids: tuple[str, ...],
    candidates: tuple[CatalogArtifactEvidence, ...],
) -> dict[str, object]:
    return {
        "account_id": account_id,
        "created_at_ms": created_at_ms,
        "expires_at_ms": expires_at_ms,
        "max_candidates": max_candidates,
        "max_total_bytes": max_total_bytes,
        "database": database,
        "registered_strategy_instance_ids": registered_strategy_instance_ids,
        "candidates": candidates,
    }


def _validate_plan(plan: CatalogQuarantinePlan, supplied_token: str) -> None:
    payload = _plan_payload(
        account_id=plan.account_id,
        created_at_ms=plan.created_at_ms,
        expires_at_ms=plan.expires_at_ms,
        max_candidates=plan.max_candidates,
        max_total_bytes=plan.max_total_bytes,
        database=plan.database,
        registered_strategy_instance_ids=plan.registered_strategy_instance_ids,
        candidates=plan.candidates,
    )
    expected = hashlib.sha256(canonical_json_bytes(_jsonable(payload))).hexdigest()
    if plan.schema_version != 1 or plan.plan_id != expected or plan.confirmation_token != expected:
        raise CatalogQuarantineRefused("catalog quarantine plan content hash does not verify")
    if not secrets.compare_digest(supplied_token, expected):
        raise CatalogQuarantineRefused("catalog quarantine confirmation token does not match")


def _validate_limits(max_candidates: int, max_total_bytes: int, ttl_ms: int) -> None:
    if max_candidates < 1 or max_total_bytes < 1 or ttl_ms < 1:
        raise CatalogQuarantineRefused("catalog quarantine bounds and TTL must be positive")


def _require_manifest(path: Path, expected: dict[str, object]) -> None:
    try:
        observed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogQuarantineRefused("prepared quarantine manifest is unreadable") from exc
    applied = {**expected, "state": "applied"}
    if observed not in (expected, applied):
        raise CatalogQuarantineRefused("prepared quarantine manifest does not match the plan")


def _require_safe_quarantine_paths(
    *,
    runner_artifacts_root: Path,
    quarantine_root: Path,
    quarantine_dir: Path,
    prepared_manifest: Path,
    receipt_path: Path,
    candidates: tuple[CatalogArtifactEvidence, ...],
) -> None:
    if runner_artifacts_root.is_symlink() or not runner_artifacts_root.is_dir():
        raise CatalogQuarantineRefused("runner artifacts root is missing or unsafe")
    for path, label in (
        (quarantine_root, "quarantine root"),
        (quarantine_dir, "quarantine directory"),
    ):
        if path.is_symlink():
            raise CatalogQuarantineRefused(f"{label} must not be a symbolic link")
        if path.exists() and not path.is_dir():
            raise CatalogQuarantineRefused(f"{label} must be a directory")
    for path, label in (
        (prepared_manifest, "quarantine manifest"),
        (receipt_path, "quarantine receipt"),
    ):
        if path.is_symlink():
            raise CatalogQuarantineRefused(f"{label} must not be a symbolic link")
    for artifact in candidates:
        destination = quarantine_dir / artifact.strategy_instance_id
        if destination.is_symlink():
            raise CatalogQuarantineRefused(
                "catalog quarantine destination must not be a symbolic link"
            )


def _remove_prepared_manifest_after_rollback(
    path: Path,
    *,
    expected: dict[str, object],
) -> None:
    try:
        observed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogQuarantineRefused(
            "rolled-back quarantine manifest cannot be safely removed"
        ) from exc
    if observed != expected:
        raise CatalogQuarantineRefused(
            "rolled-back quarantine manifest no longer matches the prepared plan"
        )
    path.unlink()
    fsync_directory(path.parent)


def _has_durably_moved_candidate(
    quarantine_dir: Path,
    candidates: tuple[CatalogArtifactEvidence, ...],
) -> bool:
    return any(
        (quarantine_dir / artifact.strategy_instance_id).is_dir()
        for artifact in candidates
    )


def _require_stopped_authority(account_dir: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = account_dir / f"{DB_FILENAME}{suffix}"
        if sidecar.is_symlink():
            raise CatalogQuarantineRefused("SQLite sidecar must not be a symbolic link")
        if sidecar.exists() and not sidecar.is_file():
            raise CatalogQuarantineRefused("SQLite sidecar must be a regular file")
    wal = account_dir / f"{DB_FILENAME}-wal"
    if wal.is_file() and wal.stat().st_size != 0:
        raise CatalogQuarantineRefused(
            "catalog quarantine requires a stopped, checkpointed SQLite authority"
        )
    db_path = account_dir / DB_FILENAME
    if not db_path.is_file() or db_path.is_symlink():
        raise CatalogQuarantineRefused("SQLite database is missing or unsafe")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT execution_lease_owner, execution_lease_expires_at_ms "
            "FROM control_meta WHERE id = 1"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise CatalogQuarantineRefused("SQLite execution lease cannot be verified") from exc
    finally:
        conn.close()
    if row is None or row[0] is not None or row[1] is not None:
        raise CatalogQuarantineRefused(
            "catalog quarantine requires the active Clerk process to release its lease"
        )


def _rollback_moves(moved: list[tuple[Path, Path]]) -> None:
    for source, destination in reversed(moved):
        if destination.exists() and not source.exists():
            os.replace(destination, source)
            fsync_directory(destination.parent)
            fsync_directory(source.parent)


def _jsonable(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
