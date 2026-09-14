"""Host-only registry backup, restore, reconciliation and rollback evidence.

The fleet registry is not custody, but restoring it is still an authority
event: a backup can predate a lane's durable confirmation.  This module keeps
the recovery hold beside the registry (not in a lane) and makes the host
ceremony reconcile the original volume marker and confirmation evidence before
normal routing or assignment mutation is admitted again.

The artifacts deliberately contain only registry and nonsecret identity
facts.  They never copy a Clerk volume, broker credential, command body,
arming record, or provider receipt.  Provider execution remains inside the
original Clerk and this code never dispatches or retries a command.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.broker.fleet.confirmation import (
    ConfirmationEvidenceError,
    read_confirmation_evidence,
)
from app.broker.fleet.errors import (
    ClerkAssignmentConflict,
    ClerkIdentityMismatch,
    FleetRegistryRecoveryPending,
    FleetRegistryUnavailable,
)
from app.broker.fleet.records import (
    AccountAssignmentRecord,
    AssignmentState,
    ProviderSummaryObservation,
)
from app.broker.fleet.store import registry_database_path
from app.utils.advisory_lock import advisory_file_lock
from app.utils.atomic_file import atomic_write_bytes, fsync_parent_dir
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from app.utils.timestamps import now_ms_utc

if TYPE_CHECKING:
    from app.broker.fleet.service import FleetControlService
    from app.broker.fleet.store import FleetRegistryStore


BACKUP_DATABASE_FILENAME = "fleet-registry.sqlite3"
BACKUP_MANIFEST_FILENAME = "fleet-registry-manifest.json"
RECOVERY_STATE_FILENAME = "registry-recovery.json"
MANIFEST_SCHEMA_VERSION = 1
D_COMPATIBLE_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class RegistryBackupManifest:
    """The nonsecret identity inventory pinned beside a registry snapshot."""

    manifest_schema_version: int
    registry_id: str
    registry_schema_version: int
    captured_at_ms: int
    database_sha256: str
    active_clerk_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegistryRecoveryState:
    """Durable hold state for a registry restored through this ceremony."""

    state_schema_version: int
    registry_id: str
    backup_sha256: str
    opened_at_ms: int
    required_clerk_ids: tuple[str, ...]
    reconciled_clerk_ids: tuple[str, ...]
    provider_summaries: dict[str, dict[str, str]]
    empty_inventory_attestation: dict[str, str | int] | None

    @property
    def mutations_closed(self) -> bool:
        """One hold: routing AND assignment mutation, closed together.

        There is no second bit. ``require_recovery_hold_clear`` is the sole
        enforcement and every mutating entry point calls it, so a restored
        registry refuses routing, provisioning, reservation, confirmation,
        release and retirement off this one derived value.
        """
        if not self.required_clerk_ids:
            return self.empty_inventory_attestation is None
        return set(self.required_clerk_ids) != set(self.reconciled_clerk_ids)


def recovery_state_path(control_dir: Path) -> Path:
    """Return the durable recovery hold path on the coordinator volume."""
    return control_dir / "fleet" / RECOVERY_STATE_FILENAME


def _sha256(path: Path) -> str:
    """Hash one artifact without loading an unbounded database into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """Write small ceremony evidence atomically and durably."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    atomic_write_bytes(path, encoded)


def _manifest_from_json(raw: object) -> RegistryBackupManifest:
    """Parse the closed manifest shape; corruption is a recovery refusal."""
    if not isinstance(raw, dict) or set(raw) != {
        "manifest_schema_version",
        "registry_id",
        "registry_schema_version",
        "captured_at_ms",
        "database_sha256",
        "active_clerk_ids",
    }:
        raise FleetRegistryUnavailable(
            "The registry backup manifest has an unknown or incomplete shape.",
            next_step="Use an intact backup produced by manage_broker_fleet backup.",
        )
    if raw["manifest_schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise FleetRegistryUnavailable(
            "The registry backup manifest was written by an incompatible recovery build.",
            next_step="Use the build that created the backup, or a documented compatible one.",
        )
    if not isinstance(raw["registry_id"], str) or not raw["registry_id"]:
        raise FleetRegistryUnavailable("The registry backup manifest has no registry identity.")
    if isinstance(raw["registry_schema_version"], bool) or not isinstance(raw["registry_schema_version"], int):
        raise FleetRegistryUnavailable("The registry backup manifest has an invalid schema version.")
    if (
        isinstance(raw["captured_at_ms"], bool)
        or not isinstance(raw["captured_at_ms"], int)
        or not 0 <= raw["captured_at_ms"] <= MAX_TIMESTAMP_MS
    ):
        raise FleetRegistryUnavailable("The registry backup manifest has an invalid capture time.")
    if (
        not isinstance(raw["database_sha256"], str)
        or len(raw["database_sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in raw["database_sha256"])
    ):
        raise FleetRegistryUnavailable("The registry backup manifest has an invalid database hash.")
    clerk_ids = raw["active_clerk_ids"]
    if not isinstance(clerk_ids, list) or any(not isinstance(item, str) or not item for item in clerk_ids):
        raise FleetRegistryUnavailable("The registry backup manifest has invalid clerk identities.")
    if len(set(clerk_ids)) != len(clerk_ids):
        raise FleetRegistryUnavailable("The registry backup manifest repeats a clerk identity.")
    return RegistryBackupManifest(
        manifest_schema_version=raw["manifest_schema_version"],
        registry_id=raw["registry_id"],
        registry_schema_version=raw["registry_schema_version"],
        captured_at_ms=raw["captured_at_ms"],
        database_sha256=raw["database_sha256"],
        active_clerk_ids=tuple(clerk_ids),
    )


def read_backup_manifest(backup_dir: Path) -> RegistryBackupManifest:
    """Read an intact backup manifest, refusing malformed or missing evidence."""
    try:
        raw = json.loads((backup_dir / BACKUP_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FleetRegistryUnavailable(
            f"The registry backup manifest in {backup_dir} is unreadable: {exc}",
            next_step="Use an intact restricted backup artifact.",
        ) from exc
    return _manifest_from_json(raw)


def create_registry_backup(store: FleetRegistryStore, *, backup_dir: Path) -> RegistryBackupManifest:
    """Snapshot the open registry and write its independently verifiable manifest."""
    database = backup_dir / BACKUP_DATABASE_FILENAME
    manifest_path = backup_dir / BACKUP_MANIFEST_FILENAME
    if database.exists() or manifest_path.exists():
        raise FleetRegistryUnavailable(
            f"Backup destination {backup_dir} already contains a registry artifact.",
            next_step="Choose a fresh, restricted backup directory; never overwrite evidence.",
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    store.backup_to(database)
    manifest = RegistryBackupManifest(
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        registry_id=store.registry_id,
        registry_schema_version=store.schema_version,
        captured_at_ms=now_ms_utc(),
        database_sha256=_sha256(database),
        # Only an effective assignment was routeable authority at capture.
        # A provisioned, reserved, draining or retired row has no confirmed
        # account/binding grant to reconcile, so making it a recovery gate
        # would deadlock a safe restore for an inactive lane.
        active_clerk_ids=tuple(
            sorted(
                {
                    assignment.clerk_id
                    for assignment in store.list_active_assignments()
                    if assignment.state.value == "effective"
                }
            )
        ),
    )
    _atomic_json(manifest_path, asdict(manifest))
    return manifest


def _validate_backup_database(
    *, backup_dir: Path, manifest: RegistryBackupManifest, max_schema_version: int
) -> Path:
    """Validate bytes and immutable database metadata before a destructive restore."""
    database = backup_dir / BACKUP_DATABASE_FILENAME
    if not database.is_file() or _sha256(database) != manifest.database_sha256:
        raise FleetRegistryUnavailable(
            "The registry backup database does not match its manifest hash.",
            next_step="Do not restore corrupted evidence; locate the original immutable backup.",
        )
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT schema_version, registry_id FROM fleet_meta WHERE id = 1"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise FleetRegistryUnavailable(
            f"The registry backup database cannot be read: {exc}",
            next_step="Do not restore it; locate an intact registry backup.",
        ) from exc
    if row is None or int(row[0]) != manifest.registry_schema_version or row[1] != manifest.registry_id:
        raise FleetRegistryUnavailable(
            "The registry backup database metadata does not match its manifest.",
            next_step="Do not restore mismatched evidence; locate its matching manifest.",
        )
    if manifest.registry_schema_version > max_schema_version:
        raise FleetRegistryUnavailable(
            f"Registry backup schema_version={manifest.registry_schema_version} is newer "
            f"than the requested compatible build ({max_schema_version}).",
            next_step="Restore with the matching fleet-aware build; never guess a downgrade.",
        )
    return database


def _write_recovery_state(control_dir: Path, state: RegistryRecoveryState) -> None:
    """Persist a recovery gate that survives coordinator process replacement."""
    _atomic_json(recovery_state_path(control_dir), asdict(state))


def read_recovery_state(control_dir: Path) -> RegistryRecoveryState | None:
    """Read the recovery gate. Missing means no restore ceremony is active."""
    target = recovery_state_path(control_dir)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise FleetRegistryRecoveryPending(
            f"The registry recovery state at {target} is unreadable: {exc}",
            next_step="Keep routing closed and restore the ceremony evidence from backup.",
        ) from exc
    required = {
        "state_schema_version", "registry_id", "backup_sha256", "opened_at_ms",
        "required_clerk_ids", "reconciled_clerk_ids", "provider_summaries",
        "empty_inventory_attestation",
    }
    if (
        not isinstance(raw, dict)
        or set(raw) != required
        or isinstance(raw.get("state_schema_version"), bool)
        or raw.get("state_schema_version") != 1
    ):
        raise FleetRegistryRecoveryPending(
            "The registry recovery state has an unknown or incomplete shape.",
            next_step="Keep routing closed and restore the ceremony evidence from backup.",
        )
    identifiers = (raw["required_clerk_ids"], raw["reconciled_clerk_ids"])
    if any(not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items) for items in identifiers):
        raise FleetRegistryRecoveryPending("The registry recovery state has invalid clerk identities.")
    if any(len(set(items)) != len(items) for items in identifiers):
        raise FleetRegistryRecoveryPending("The registry recovery state repeats a clerk identity.")
    if not set(raw["reconciled_clerk_ids"]).issubset(raw["required_clerk_ids"]):
        raise FleetRegistryRecoveryPending("The registry recovery state reconciles an unknown clerk.")
    summaries = raw["provider_summaries"]
    if not isinstance(summaries, dict) or set(summaries) != set(raw["reconciled_clerk_ids"]):
        raise FleetRegistryRecoveryPending("The registry recovery state has incomplete provider summaries.")
    if not all(isinstance(value, dict) for value in summaries.values()):
        raise FleetRegistryRecoveryPending("The registry recovery state has invalid provider summaries.")
    try:
        for value in summaries.values():
            ProviderSummaryObservation.parse(value)
    except ValueError as exc:
        raise FleetRegistryRecoveryPending(
            f"The registry recovery state has an invalid provider summary: {exc}",
        ) from exc
    attestation = raw["empty_inventory_attestation"]
    if attestation is not None and (
            raw["required_clerk_ids"]
            or not isinstance(attestation, dict)
            or set(attestation) != {"operator", "change_ref", "attested_at_ms"}
            or not isinstance(attestation["operator"], str)
            or not attestation["operator"].strip()
            or len(attestation["operator"]) > 128
            or not isinstance(attestation["change_ref"], str)
            or not attestation["change_ref"].strip()
            or len(attestation["change_ref"]) > 512
            or isinstance(attestation["attested_at_ms"], bool)
            or not isinstance(attestation["attested_at_ms"], int)
            or not 0 <= attestation["attested_at_ms"] <= MAX_TIMESTAMP_MS
    ):
        raise FleetRegistryRecoveryPending(
            "The registry recovery state has an invalid empty-inventory attestation."
        )
    if (
        not isinstance(raw["registry_id"], str)
        or not raw["registry_id"]
        or not isinstance(raw["backup_sha256"], str)
        or len(raw["backup_sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in raw["backup_sha256"])
        or isinstance(raw["opened_at_ms"], bool)
        or not isinstance(raw["opened_at_ms"], int)
        or not 0 <= raw["opened_at_ms"] <= MAX_TIMESTAMP_MS
    ):
        raise FleetRegistryRecoveryPending("The registry recovery state has invalid identity evidence.")
    return RegistryRecoveryState(
        state_schema_version=1,
        registry_id=raw["registry_id"],
        backup_sha256=raw["backup_sha256"],
        opened_at_ms=raw["opened_at_ms"],
        required_clerk_ids=tuple(raw["required_clerk_ids"]),
        reconciled_clerk_ids=tuple(raw["reconciled_clerk_ids"]),
        provider_summaries=summaries,
        empty_inventory_attestation=attestation,
    )


def restore_registry_backup(
    *, control_dir: Path, backup_dir: Path, max_schema_version: int
) -> RegistryBackupManifest:
    """Restore an exact snapshot and immediately persist a closed recovery gate.

    The previous registry is retained beside the live file before replacement,
    so an operator can investigate without deleting history.  The restored
    snapshot is never opened as a blank registry and the hold is written before
    this function returns.
    """
    manifest = read_backup_manifest(backup_dir)
    database = _validate_backup_database(
        backup_dir=backup_dir, manifest=manifest, max_schema_version=max_schema_version
    )
    target = registry_database_path(control_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Store operations and replacement share this lock. An in-flight operation
    # finishes before recovery starts; afterward, every pre-restore Store sees
    # the new path identity and refuses until its coordinator restarts.
    with advisory_file_lock(target):
        # Write the closed gate *before* replacing the database. A crash between
        # these two durable actions leaves either the old registry or the restored
        # one closed; it can never leave a restored registry briefly routable.
        _write_recovery_state(
            control_dir,
            RegistryRecoveryState(
                state_schema_version=1,
                registry_id=manifest.registry_id,
                backup_sha256=manifest.database_sha256,
                opened_at_ms=now_ms_utc(),
                required_clerk_ids=manifest.active_clerk_ids,
                reconciled_clerk_ids=(),
                provider_summaries={},
                empty_inventory_attestation=None,
            ),
        )
        if target.exists():
            preserved = target.with_name(f"{target.name}.before-restore-{now_ms_utc()}")
            source = sqlite3.connect(target)
            destination = sqlite3.connect(preserved)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
        temporary = target.with_name(f"{target.name}.restore-{os.getpid()}")
        shutil.copy2(database, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        for suffix in ("-wal", "-shm"):
            target.with_name(f"{target.name}{suffix}").unlink(missing_ok=True)
        fsync_parent_dir(target)
    return manifest


def require_recovery_hold_clear(control_dir: Path, *, registry_id: str) -> None:
    """Fail closed while a restore ceremony has unresolved original lanes."""
    state = read_recovery_state(control_dir)
    if state is None:
        return
    if state.registry_id != registry_id:
        raise FleetRegistryRecoveryPending(
            "The recovery evidence names a different registry identity.",
            next_step="Keep routing closed and repeat restore from a matching backup pair.",
        )
    if state.mutations_closed:
        remaining = sorted(set(state.required_clerk_ids) - set(state.reconciled_clerk_ids))
        if not remaining:
            raise FleetRegistryRecoveryPending(
                "Registry recovery is awaiting explicit host closeout for an empty active-lane inventory.",
                next_step="Verify that the restored registry has no effective assignments, then run "
                "closeout-empty-registry with the restricted incident/change reference.",
            )
        raise FleetRegistryRecoveryPending(
            "Registry recovery is still awaiting original lane reconciliation: "
            + ", ".join(remaining),
            next_step="Run reconcile-registry for each original Clerk volume; do not "
            "create, release, or reassign an account while recovery is pending.",
        )


def closeout_empty_registry_recovery(
    store: FleetRegistryStore,
    *,
    control_dir: Path,
    operator: str,
    change_ref: str,
) -> RegistryRecoveryState:
    """Record explicit host closeout when a restored backup had no active lanes."""
    state = read_recovery_state(control_dir)
    if state is None or state.registry_id != store.registry_id:
        raise FleetRegistryRecoveryPending(
            "No matching registry restore ceremony is awaiting empty-inventory closeout."
        )
    if state.required_clerk_ids or state.reconciled_clerk_ids or state.provider_summaries:
        raise FleetRegistryRecoveryPending(
            "Empty-inventory closeout cannot replace original lane reconciliation."
        )
    operator = operator.strip()
    change_ref = change_ref.strip()
    if not operator or len(operator) > 128 or not change_ref or len(change_ref) > 512:
        raise FleetRegistryRecoveryPending(
            "Empty-inventory closeout requires bounded operator and incident/change references."
        )
    effective = [
        assignment
        for assignment in store.list_active_assignments()
        if assignment.state == AssignmentState.EFFECTIVE
    ]
    if effective:
        raise FleetRegistryRecoveryPending(
            "The restored registry contains effective assignments and cannot use empty-inventory closeout.",
            next_step="Restore matching lane evidence and reconcile every effective assignment.",
        )
    updated = RegistryRecoveryState(
        state_schema_version=state.state_schema_version,
        registry_id=state.registry_id,
        backup_sha256=state.backup_sha256,
        opened_at_ms=state.opened_at_ms,
        required_clerk_ids=(),
        reconciled_clerk_ids=(),
        provider_summaries={},
        empty_inventory_attestation={
            "operator": operator,
            "change_ref": change_ref,
            "attested_at_ms": now_ms_utc(),
        },
    )
    _write_recovery_state(control_dir, updated)
    return updated


def reconcile_restored_lane(
    service: FleetControlService,
    *,
    clerk_id: str,
    volume_root: Path,
    provider_summary: dict[str, object],
) -> RegistryRecoveryState:
    """Reconcile one original Clerk's durable facts into a restored registry.

    This is the only assignment write allowed during a recovery hold.  It can
    only restore the same clerk/account/generation from its own marked volume;
    it cannot release, transfer, mint identities, or submit an execution
    command.  A conflicting restored row is a refusal, not a reason to choose
    whichever evidence looks newest.
    """
    control_dir = service._store.db_path.parent.parent
    state = read_recovery_state(control_dir)
    if state is None:
        raise FleetRegistryRecoveryPending(
            "No registry restore ceremony is active.",
            next_step="Use reconcile-registry only after restore-registry or rollback-d-compatible.",
        )
    if state.registry_id != service._store.registry_id:
        raise FleetRegistryRecoveryPending(
            "The active registry does not match the recovery ceremony identity.",
            next_step="Keep routing closed and restore a matching registry backup.",
        )
    if clerk_id not in state.required_clerk_ids:
        raise ClerkIdentityMismatch(
            f"Clerk {clerk_id} was not present in the restored registry backup.",
            next_step="Do not import an unknown lane into a restored registry; recover a newer backup.",
        )
    clerk = service._require_clerk(clerk_id)
    service.verify_clerk_volume(clerk_id=clerk_id, volume_root=volume_root)
    try:
        evidence = read_confirmation_evidence(volume_root)
    except ConfirmationEvidenceError:
        raise
    if evidence is None:
        raise ClerkIdentityMismatch(
            f"Clerk {clerk_id} has no durable confirmation evidence on its own volume.",
            next_step="Keep routing closed; recover the lane with its provider procedure first.",
        )
    if (
        evidence.clerk_id != clerk.clerk_id
        or evidence.volume_id != clerk.volume_id
        or evidence.registry_id != service._store.registry_id
    ):
        raise ClerkIdentityMismatch(
            f"Clerk {clerk_id}'s durable evidence does not match the restored registry and volume.",
            next_step="Keep routing closed; use the original registry and lane-volume backup pair.",
        )
    canonical = service._adapter(clerk.broker).canonical_account_id(evidence.canonical_account_id)
    if canonical != evidence.canonical_account_id:
        raise ClerkIdentityMismatch(
            f"Clerk {clerk_id}'s evidence carries a noncanonical account identity.",
            next_step="Keep routing closed and recover the provider-owned evidence.",
        )
    try:
        summary = ProviderSummaryObservation.parse(provider_summary)
    except ValueError as exc:
        raise ClerkIdentityMismatch(
            f"Clerk {clerk_id}'s provider summary confirmation is invalid: {exc}",
            next_step="Supply the provider's bounded endpoint-mode and authority-state summary.",
        ) from exc
    assert summary is not None
    now = now_ms_utc()
    try:
        with service._store.transaction() as conn:
            existing = service._store.read_assignment_on(
                conn, broker=clerk.broker, canonical_account_id=canonical
            )
            restored = AccountAssignmentRecord(
                broker=clerk.broker,
                canonical_external_account_id=canonical,
                clerk_id=clerk_id,
                assignment_generation=evidence.assignment_generation,
                state=AssignmentState.EFFECTIVE,
                effective_profile_id=evidence.effective_profile_id,
                effective_revision=evidence.effective_revision,
                confirmed_binding_generation=evidence.binding_generation,
                confirmed_profile_id=evidence.effective_profile_id,
                confirmed_revision=evidence.effective_revision,
                confirmed_at_ms=evidence.confirmed_at_ms,
                confirmed_agent_instance_id=evidence.agent_instance_id,
                confirmed_routing_epoch=evidence.routing_epoch,
                recorded_at_ms=now if existing is None else existing.recorded_at_ms,
                updated_at_ms=now,
            )
            if existing is None:
                service._store.insert_assignment(conn, restored)
            else:
                if (
                    existing.clerk_id != clerk_id
                    or existing.state == AssignmentState.RELEASED
                    or existing.assignment_generation != evidence.assignment_generation
                ):
                    raise ClerkAssignmentConflict(
                        f"Restored assignment {canonical} conflicts with durable evidence "
                        f"from clerk {clerk_id}.",
                        next_step="Keep routing closed; resolve the conflicting recovery "
                        "evidence offline.",
                    )
                if (
                    existing.confirmed_binding_generation is not None
                    and existing.confirmed_binding_generation > evidence.binding_generation
                ) or (
                    existing.confirmed_routing_epoch is not None
                    and existing.confirmed_routing_epoch > evidence.routing_epoch
                ):
                    raise ClerkIdentityMismatch(
                        f"Restored registry facts for clerk {clerk_id} are newer than its "
                        "lane evidence.",
                        next_step="Keep routing closed; use a matching lane backup rather "
                        "than rolling facts back.",
                    )
                if not service._store.cas_update_assignment(
                    conn,
                    restored,
                    previous_generation=existing.assignment_generation,
                    previous_state=existing.state,
                ):
                    raise ClerkAssignmentConflict(
                        f"Assignment {canonical} changed while registry recovery was "
                        "reconciling it.",
                    )
    except sqlite3.IntegrityError as exc:
        raise ClerkIdentityMismatch(
            f"Clerk {clerk_id}'s durable recovery evidence violates the registry protocol.",
            next_step="Keep routing closed and recover an intact matching lane-evidence backup.",
        ) from exc
    reconciled = tuple(sorted(set(state.reconciled_clerk_ids) | {clerk_id}))
    summaries = dict(state.provider_summaries)
    summaries[clerk_id] = json.loads(summary.to_json())
    updated = RegistryRecoveryState(
        state_schema_version=state.state_schema_version,
        registry_id=state.registry_id,
        backup_sha256=state.backup_sha256,
        opened_at_ms=state.opened_at_ms,
        required_clerk_ids=state.required_clerk_ids,
        reconciled_clerk_ids=reconciled,
        provider_summaries=summaries,
        empty_inventory_attestation=state.empty_inventory_attestation,
    )
    _write_recovery_state(control_dir, updated)
    return updated


__all__ = [
    "BACKUP_DATABASE_FILENAME",
    "BACKUP_MANIFEST_FILENAME",
    "D_COMPATIBLE_SCHEMA_VERSION",
    "RegistryBackupManifest",
    "RegistryRecoveryState",
    "closeout_empty_registry_recovery",
    "create_registry_backup",
    "read_backup_manifest",
    "read_recovery_state",
    "reconcile_restored_lane",
    "require_recovery_hold_clear",
    "restore_registry_backup",
]
