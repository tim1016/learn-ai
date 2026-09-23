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
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.broker.fleet.errors import FleetControlError
from app.broker.fleet.store import registry_database_path
from app.broker.fleet.volume import read_volume_marker, verify_volume_identity
from app.installation_migration.contents import BUNDLED_VOLUMES
from app.installation_migration.errors import MigrationRefused
from app.installation_migration.tree import extract_tar

#: The Alpaca clerk's account-database layout under its volume root:
#: ``accounts/alpaca/<account_id>/clerk.db``. Duplicated from
#: ``app.broker.alpaca.clerk.sqlite.writes.confined_account_file`` and
#: ``repository.DB_FILENAME`` because importing that package needs the data
#: plane's settings, which a host tool does not have; a parity test in
#: ``tests/installation_migration/test_facts.py`` pins both.
CLERK_ACCOUNTS_RELATIVE = Path("accounts") / "alpaca"
CLERK_DB_FILENAME = "clerk.db"

_SQLITE_SIDECARS = ("-wal", "-shm")


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


def read_registry_facts(control_root: Path) -> dict[str, Any]:
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
    return {
        "registry_id": meta["registry_id"],
        "schema_version": meta["schema_version"],
        "clerks": rows["clerks"],
        "assignments": rows["assignments"],
        "approved_endpoints": rows["approved_endpoints"],
    }


def read_clerk_volume_facts(volume_name: str, volume_root: Path) -> dict[str, Any]:
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
    accounts: list[dict[str, Any]] = []
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
            accounts.append(rows[0])
    return {"volume": volume_name, "marker": asdict(marker), "accounts": accounts}


def staged_identity_facts(
    volume_tars: Mapping[str, Path], scratch: Path
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
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
    clerk_volumes = [
        read_clerk_volume_facts(volume.name, roots[volume.name])
        for volume in BUNDLED_VOLUMES
        if volume.role == "clerk"
    ]
    for clerk in registry["clerks"]:
        if clerk["lifecycle_state"] == "retired":
            continue
        root = roots.get(clerk["attestation_id"])
        if root is None:
            raise MigrationRefused(
                "clerk_volume_not_bundled",
                f"Clerk {clerk['clerk_id']} lives on volume {clerk['attestation_id']!r}, "
                "which the bundle does not carry; a lane is never moved without its volume.",
                details={"clerk_id": clerk["clerk_id"], "volume": clerk["attestation_id"]},
            )
        try:
            verify_volume_identity(
                root,
                expected_broker=clerk["broker"],
                expected_clerk_id=clerk["clerk_id"],
                expected_volume_id=clerk["volume_id"],
                expected_attestation_kind=clerk["attestation_kind"],
                expected_attestation_id=clerk["attestation_id"],
            )
        except FleetControlError as exc:
            raise MigrationRefused(
                "clerk_volume_identity_mismatch",
                f"Volume {clerk['attestation_id']} does not prove clerk "
                f"{clerk['clerk_id']}'s identity: {exc.message}",
                details={"clerk_id": clerk["clerk_id"], "volume": clerk["attestation_id"]},
            ) from exc
    return registry, clerk_volumes


__all__ = [
    "CLERK_ACCOUNTS_RELATIVE",
    "CLERK_DB_FILENAME",
    "read_clerk_volume_facts",
    "read_registry_facts",
    "staged_identity_facts",
]
