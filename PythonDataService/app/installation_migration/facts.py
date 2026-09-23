"""Identity facts read from a bundled volume's content (#2268).

Export records these in the manifest; import re-reads them from the staged
bundle and requires equality **before anything is restored and before any
schema upgrade can run** — so every read here is a raw ``SELECT`` against a
private copy of the database, never an open through the registry store or the
clerk repository, both of which migrate a schema on open.

Registry facts come from the coordinator control volume; clerk facts from a
lane volume — its identity marker (read through the canonical
:func:`app.broker.fleet.volume.read_volume_marker`, carried intact and never
re-stamped) and each account database's authority generation.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.broker.fleet.errors import FleetControlError
from app.broker.fleet.store import registry_database_path
from app.broker.fleet.volume import read_volume_marker, verify_volume_identity
from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateCorruptError,
    DesiredStateRepo,
    stable_desired_state_path,
)
from app.installation_migration.contents import BUNDLED_VOLUMES
from app.installation_migration.errors import MigrationRefused
from app.installation_migration.tree import extract_tar, root_member_bytes
from app.utils.session_anchors import MAX_TIMESTAMP_MS

#: The Alpaca clerk's account-database layout under its volume root:
#: ``accounts/alpaca/<account_id>/clerk.db``. Duplicated from
#: ``app.broker.alpaca.clerk.sqlite.writes.confined_account_file`` and
#: ``repository.DB_FILENAME`` because importing that package needs the data
#: plane's settings, which a host tool does not have; a parity test in
#: ``tests/installation_migration/test_facts.py`` pins both.
CLERK_ACCOUNTS_RELATIVE = Path("accounts") / "alpaca"
CLERK_DB_FILENAME = "clerk.db"

_SQLITE_SIDECARS = ("-wal", "-shm")


#: An instant in the domain's admissible range (temporal-rigor.md).
InstantMs = Annotated[int, Field(ge=0, le=MAX_TIMESTAMP_MS)]


class StrictRecord(BaseModel):
    """A frozen, closed, strictly typed fact: no coercion, no unknown field.

    Strict so a manifest that says ``1`` where a flag belongs, or ``true``
    where a generation belongs, is refused rather than read as the other.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class RegistryClerk(StrictRecord):
    """One clerk row of the fleet registry, as identity compares it."""

    clerk_id: str
    broker: str
    volume_id: str
    volume_root: str
    deployment_namespace: str
    attestation_kind: str
    attestation_id: str
    lifecycle_state: str


class RegistryAssignment(StrictRecord):
    """One account assignment and its generations."""

    broker: str
    account_id: str
    clerk_id: str
    assignment_generation: int
    state: str
    confirmed_binding_generation: int | None


class RegistryEndpoint(StrictRecord):
    """One approved agent endpoint."""

    endpoint_ref: str
    clerk_id: str
    base_url: str


class RegistryFacts(StrictRecord):
    """The fleet registry's identity, clerks, assignments and approved endpoints."""

    registry_id: str
    schema_version: int
    clerks: tuple[RegistryClerk, ...]
    assignments: tuple[RegistryAssignment, ...]
    approved_endpoints: tuple[RegistryEndpoint, ...]


class VolumeMarkerFacts(StrictRecord):
    """A lane volume's identity marker, exactly as the canonical reader returns it."""

    marker_version: int
    broker: str
    clerk_id: str
    volume_id: str
    attestation_kind: str
    attestation_id: str
    created_at_ms: InstantMs


class ClerkAccountFacts(StrictRecord):
    """One account database's identity and authority generation."""

    account_id: str
    authority_generation: int
    schema_version: int
    db_identity_token: str


class ClerkVolumeFacts(StrictRecord):
    """A lane volume's marker and each account database's generation."""

    volume: str
    marker: VolumeMarkerFacts
    accounts: tuple[ClerkAccountFacts, ...]


def _query_copy(database: Path, queries: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Run read-only queries against a private copy of ``database`` (and its WAL)."""
    with tempfile.TemporaryDirectory(prefix="migrate-facts-") as scratch:
        copy = Path(scratch) / database.name
        shutil.copy2(database, copy)
        for suffix in _SQLITE_SIDECARS:
            sidecar = database.with_name(f"{database.name}{suffix}")
            if sidecar.exists():
                shutil.copy2(sidecar, Path(scratch) / sidecar.name)
        try:
            connection = sqlite3.connect(copy)
        except sqlite3.DatabaseError as exc:
            raise MigrationRefused(
                "database_unreadable",
                f"{database} could not be opened: {exc}",
                details={"database": str(database)},
            ) from exc
        connection.row_factory = sqlite3.Row
        try:
            return {
                name: [dict(row) for row in connection.execute(sql).fetchall()]
                for name, sql in queries.items()
            }
        except sqlite3.DatabaseError as exc:
            raise MigrationRefused(
                "database_unreadable",
                f"{database} could not be read: {exc}",
                details={"database": str(database)},
            ) from exc
        finally:
            connection.close()


def read_registry_facts(control_root: Path) -> RegistryFacts:
    """The fleet registry's identity, clerks, assignments and approved endpoints."""
    database = registry_database_path(control_root)
    if not database.is_file():
        raise MigrationRefused(
            "registry_missing",
            f"The coordinator control volume holds no fleet registry at {database}.",
            details={"database": str(database)},
        )
    rows = _query_copy(
        database,
        {
            "meta": "SELECT registry_id, schema_version FROM fleet_meta WHERE id = 1",
            "clerks": (
                "SELECT clerk_id, broker, volume_id, volume_root, deployment_namespace, "
                "volume_attestation_kind AS attestation_kind, "
                "volume_attestation_id AS attestation_id, lifecycle_state "
                "FROM clerks ORDER BY clerk_id"
            ),
            "assignments": (
                "SELECT broker, canonical_external_account_id AS account_id, clerk_id, "
                "assignment_generation, state, confirmed_binding_generation "
                "FROM account_assignments ORDER BY broker, canonical_external_account_id"
            ),
            "approved_endpoints": (
                "SELECT endpoint_ref, clerk_id, base_url FROM approved_endpoints "
                "ORDER BY endpoint_ref"
            ),
        },
    )
    if len(rows["meta"]) != 1:
        raise MigrationRefused(
            "registry_unreadable",
            f"The fleet registry at {database} has no identity row.",
            details={"database": str(database)},
        )
    meta = rows["meta"][0]
    try:
        return RegistryFacts(
            registry_id=meta["registry_id"],
            schema_version=meta["schema_version"],
            clerks=tuple(RegistryClerk(**row) for row in rows["clerks"]),
            assignments=tuple(RegistryAssignment(**row) for row in rows["assignments"]),
            approved_endpoints=tuple(
                RegistryEndpoint(**row) for row in rows["approved_endpoints"]
            ),
        )
    except ValidationError as exc:
        raise MigrationRefused(
            "registry_unreadable",
            f"The fleet registry at {database} holds a value of an unexpected type: {exc}",
            details={"database": str(database)},
        ) from exc


def read_clerk_volume_facts(volume_name: str, volume_root: Path) -> ClerkVolumeFacts:
    """A lane volume's identity marker and each account's authority generation."""
    try:
        marker = read_volume_marker(volume_root)
    except FleetControlError as exc:
        raise MigrationRefused(
            "clerk_volume_marker_unreadable",
            f"Volume {volume_name}'s identity marker is unreadable: {exc.message}",
            details={"volume": volume_name},
        ) from exc
    if marker is None:
        raise MigrationRefused(
            "clerk_volume_unmarked",
            f"Volume {volume_name} carries no clerk identity marker; a lane volume "
            "is moved with the identity it was provisioned with, never without one.",
            details={"volume": volume_name},
        )
    accounts: list[ClerkAccountFacts] = []
    accounts_root = volume_root / CLERK_ACCOUNTS_RELATIVE
    if accounts_root.is_dir():
        for account_dir in sorted(accounts_root.iterdir()):
            database = account_dir / CLERK_DB_FILENAME
            if not database.is_file():
                continue
            rows = _query_copy(
                database,
                {
                    "meta": (
                        "SELECT account_id, authority_generation, schema_version, "
                        "db_identity_token FROM control_meta WHERE id = 1"
                    )
                },
            )["meta"]
            if len(rows) != 1:
                raise MigrationRefused(
                    "clerk_database_unreadable",
                    f"{database} in volume {volume_name} has no control_meta row.",
                    details={"volume": volume_name, "database": str(database)},
                )
            try:
                accounts.append(ClerkAccountFacts(**rows[0]))
            except ValidationError as exc:
                raise MigrationRefused(
                    "clerk_database_unreadable",
                    f"{database} in volume {volume_name} holds a control_meta value of "
                    f"an unexpected type: {exc}",
                    details={"volume": volume_name, "database": str(database)},
                ) from exc
    return ClerkVolumeFacts(
        volume=volume_name,
        marker=VolumeMarkerFacts(**asdict(marker)),
        accounts=tuple(accounts),
    )


@dataclass(frozen=True, slots=True)
class StagedIdentity:
    """Identity facts read from staged volume tars, and where they were read."""

    registry: RegistryFacts
    clerk_volumes: tuple[ClerkVolumeFacts, ...]
    lane_roots: Mapping[str, Path]


def staged_identity_facts(volume_tars: Mapping[str, Path], scratch: Path) -> StagedIdentity:
    """Registry and lane-volume facts read from staged volume tars.

    Shared by export (facts for the manifest) and import (facts to compare
    with it), so both sides read identity the same way. Every live clerk in
    the registry must travel in a bundled lane volume whose marker proves
    that clerk's identity through the canonical volume gate.
    """
    roots: dict[str, Path] = {}
    for volume in BUNDLED_VOLUMES:
        if volume.role in ("fleet_control", "clerk"):
            root = scratch / "extracted" / volume.name
            root.mkdir(parents=True)
            extract_tar(volume_tars[volume.name], root)
            roots[volume.name] = root
    control = next(v.name for v in BUNDLED_VOLUMES if v.role == "fleet_control")
    registry = read_registry_facts(roots[control])
    clerk_volumes = tuple(
        read_clerk_volume_facts(volume.name, roots[volume.name])
        for volume in BUNDLED_VOLUMES
        if volume.role == "clerk"
    )
    for clerk in registry.clerks:
        if clerk.lifecycle_state == "retired":
            continue
        root = roots.get(clerk.attestation_id)
        if root is None:
            raise MigrationRefused(
                "clerk_volume_not_bundled",
                f"Clerk {clerk.clerk_id} lives on volume {clerk.attestation_id!r}, "
                "which the bundle does not carry; a lane is never moved without its volume.",
                details={"clerk_id": clerk.clerk_id, "volume": clerk.attestation_id},
            )
        try:
            verify_volume_identity(
                root,
                expected_broker=clerk.broker,
                expected_clerk_id=clerk.clerk_id,
                expected_volume_id=clerk.volume_id,
                expected_attestation_kind=clerk.attestation_kind,
                expected_attestation_id=clerk.attestation_id,
            )
        except FleetControlError as exc:
            raise MigrationRefused(
                "clerk_volume_identity_mismatch",
                f"Volume {clerk.attestation_id} does not prove clerk "
                f"{clerk.clerk_id}'s identity: {exc.message}",
                details={"clerk_id": clerk.clerk_id, "volume": clerk.attestation_id},
            ) from exc
    lane_roots = {
        volume.name: roots[volume.name] for volume in BUNDLED_VOLUMES if volume.role == "clerk"
    }
    return StagedIdentity(registry=registry, clerk_volumes=clerk_volumes, lane_roots=lane_roots)


#: The per-bot state directory under a lane's artifact root, the layout
#: ``stable_desired_state_path`` writes (``<root>/live_state/<sid>/…``).
_LIVE_STATE_DIRECTORY = "live_state"


def bots_not_stopped(lane_roots: Mapping[str, Path]) -> list[dict[str, str]]:
    """Every bot in a copied lane volume whose durable desired state is not STOPPED.

    Read through the canonical desired-state repository, from the copy that
    will travel: a bot started between the quiet read and ``podman stop``
    would otherwise restart on the new host.
    """
    found: list[dict[str, str]] = []
    for volume, root in sorted(lane_roots.items()):
        live_state = root / _LIVE_STATE_DIRECTORY
        if not live_state.is_dir():
            continue
        for bot_dir in sorted(child for child in live_state.iterdir() if child.is_dir()):
            try:
                path = stable_desired_state_path(root, bot_dir.name)
                if not path.is_file():
                    continue
                state = DesiredStateRepo(path).read_state()
            except (ValueError, DesiredStateCorruptError) as exc:
                raise MigrationRefused(
                    "bot_desired_state_unreadable",
                    f"Bot {bot_dir.name!r} in volume {volume} has no readable desired "
                    f"state: {exc}",
                    details={"volume": volume, "strategy_instance_id": bot_dir.name},
                ) from exc
            if state is not DesiredState.STOPPED:
                found.append(
                    {
                        "volume": volume,
                        "strategy_instance_id": bot_dir.name,
                        "desired_state": state.value,
                    }
                )
    return found


class PostgresFacts(StrictRecord):
    """What the bundled database cluster says about itself."""

    pg_version: str


_PG_VERSION = "PG_VERSION"
_POSTMASTER_PID = "postmaster.pid"


def read_postgres_facts(pg_tar: Path, *, volume: str) -> PostgresFacts:
    """The cluster's major version; refuses a copy of a cluster still running.

    A ``postmaster.pid`` in the copy means Postgres did not shut down
    cleanly, so the copy may need crash recovery on the new host — not a
    copy of quiescent data.
    """
    files = root_member_bytes(pg_tar, (_PG_VERSION, _POSTMASTER_PID))
    if _POSTMASTER_PID in files:
        raise MigrationRefused(
            "postgres_not_cleanly_stopped",
            f"The copy of {volume} holds {_POSTMASTER_PID}: Postgres did not shut down "
            "cleanly. Start the database container, stop it cleanly, then retry.",
            details={"volume": volume},
        )
    version = files.get(_PG_VERSION, b"").decode("utf-8", errors="replace").strip()
    if not version:
        raise MigrationRefused(
            "postgres_version_unknown",
            f"The copy of {volume} has no {_PG_VERSION}; it is not a Postgres cluster.",
            details={"volume": volume},
        )
    return PostgresFacts(pg_version=version)


__all__ = [
    "CLERK_ACCOUNTS_RELATIVE",
    "CLERK_DB_FILENAME",
    "ClerkAccountFacts",
    "ClerkVolumeFacts",
    "PostgresFacts",
    "RegistryAssignment",
    "RegistryClerk",
    "RegistryEndpoint",
    "RegistryFacts",
    "StagedIdentity",
    "StrictRecord",
    "VolumeMarkerFacts",
    "bots_not_stopped",
    "read_clerk_volume_facts",
    "read_postgres_facts",
    "read_registry_facts",
    "staged_identity_facts",
]
