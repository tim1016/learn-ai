"""What the committed fleet topology tells a migration about a host (#2268).

Read from the checkout's own ``deploy/fleet/topology.snapshot.json`` — the
rendered, secret-free projection of ``compose.yaml`` + ``compose.fleet.dev.yaml``
— so the old host and the new one each answer from the code they run:

- which containers write a bundled host folder (they are stopped before the
  copy, and must be stopped before a restore);
- which ``deploy/fleet/env/*.env`` files the stack cannot start without
  (import refuses until the operator has copied them by hand);
- where each live clerk's volume is mounted, and under which namespace
  clerks register (:class:`HostTopologyFacts`) — recorded by export on the
  old host, re-read by import on the new one, and compared, so a re-approval
  is required exactly when the host changed what the registry's
  ``volume_root`` and approved endpoint refer to (#2269). Reported, never
  performed.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import dotenv_values

from app.installation_migration.errors import MigrationRefused
from app.installation_migration.records import StrictRecord

if TYPE_CHECKING:
    from app.installation_migration.facts import RegistryFacts

TOPOLOGY_SNAPSHOT = Path("deploy") / "fleet" / "topology.snapshot.json"
FLEET_OVERLAY = Path("compose.fleet.dev.yaml")
FLEET_ENV_DIRECTORY = Path("deploy") / "fleet" / "env"
#: The two non-fleet ``.env`` files the stack cannot start without: the
#: repo-root one Compose interpolates from and the data plane's own. Import
#: checks only that they exist; their values are never read or printed.
HOST_ENV_FILES = (Path(".env"), Path("PythonDataService") / ".env")
NAMESPACE_ENV = "FLEET_DEPLOYMENT_NAMESPACE"

_DEFAULTED = re.compile(r"^\$\{[A-Z0-9_]+:-(?P<default>[^}]+)\}$")
_NAMESPACE_DEFAULT = re.compile(
    r"FLEET_DEPLOYMENT_NAMESPACE:\s*\$\{FLEET_DEPLOYMENT_NAMESPACE:-(?P<default>[^}]+)\}"
)


def load_topology(repo_root: Path) -> dict[str, Any]:
    """The checkout's committed topology snapshot."""
    path = repo_root / TOPOLOGY_SNAPSHOT
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationRefused(
            "topology_unreadable",
            f"The committed fleet topology at {path} could not be read: {exc}",
            details={"path": str(path)},
        ) from exc


def _mounts(service: Mapping[str, Any]) -> Iterable[tuple[str, str, str]]:
    """(source, target, kind) for each of a service's rendered mounts."""
    for mount in service.get("volumes") or []:
        source, _, rest = str(mount).partition("->")
        target, _, kind = rest.rpartition(":")
        yield source, target, kind


def containers_writing_folders(
    topology: Mapping[str, Any], folder_keys: Iterable[str]
) -> list[str]:
    """Every container that bind-mounts a bundled folder or a path inside one."""
    keys = tuple(folder_keys)
    names: list[str] = []
    for service_name, service in sorted(topology["service_detail"].items()):
        for source, _target, kind in _mounts(service):
            if kind == "bind" and any(
                source == key or source.startswith(f"{key}/") for key in keys
            ):
                names.append(str(service.get("container_name") or service_name))
                break
    return names


def required_fleet_env_files(repo_root: Path, topology: Mapping[str, Any]) -> list[Path]:
    """Each ``deploy/fleet/env/*.env`` a fleet service reads, resolved on this host."""
    required: set[Path] = set()
    for service in topology["service_detail"].values():
        for declared in service.get("env_file") or []:
            match = _DEFAULTED.match(str(declared))
            path = Path((match.group("default") if match else str(declared)).removeprefix("./"))
            if path.parent == FLEET_ENV_DIRECTORY and path.suffix == ".env":
                required.add(repo_root / path)
    return sorted(required)


def required_env_files(repo_root: Path, topology: Mapping[str, Any]) -> list[Path]:
    """Every env file the operator copies by hand: the fleet's and the host's."""
    return sorted(
        {*required_fleet_env_files(repo_root, topology), *(repo_root / f for f in HOST_ENV_FILES)}
    )


_IMAGE_MAJOR = re.compile(r":(?P<major>\d+)(?:[.\-@][^/]*)?$")


def postgres_image_major(topology: Mapping[str, Any], *, volume_key: str) -> str | None:
    """The major version of the Postgres image that mounts ``volume_key``.

    ``None`` when no service mounts it or its image tag names no major
    (``latest``, a digest): the caller then reports rather than compares.
    """
    for service in topology["service_detail"].values():
        if any(
            source == volume_key and kind == "volume" for source, _target, kind in _mounts(service)
        ):
            match = _IMAGE_MAJOR.search(str(service.get("image") or ""))
            return None if match is None else match["major"]
    return None


def compose_variable(repo_root: Path, name: str) -> str | None:
    """A variable exactly as Compose interpolates ``${NAME:-default}`` here.

    Compose reads the process environment first and the repo-root ``.env``
    only for a name the environment does not set — a name set to empty in
    the environment still shadows ``.env``. ``.env`` is parsed by
    ``python-dotenv``, so ``export NAME=…``, quotes and inline comments read
    as Compose reads them. An empty result is ``None``: ``:-`` then takes
    the default.
    """
    if name in os.environ:
        value: str | None = os.environ[name]
    else:
        dotenv = repo_root / ".env"
        value = dotenv_values(dotenv).get(name) if dotenv.is_file() else None
    stripped = (value or "").strip()
    return stripped or None


def deployment_namespace(repo_root: Path) -> str:
    """The namespace the fleet overlay will register clerks under on this host.

    Resolved the way Compose resolves the overlay's
    ``${FLEET_DEPLOYMENT_NAMESPACE:-…}`` (:func:`compose_variable`), then the
    committed default.
    """
    configured = compose_variable(repo_root, NAMESPACE_ENV)
    if configured is not None:
        return configured
    overlay = repo_root / FLEET_OVERLAY
    match = _NAMESPACE_DEFAULT.search(overlay.read_text(encoding="utf-8"))
    if match is None:
        raise MigrationRefused(
            "topology_unreadable",
            f"{overlay} declares no default {NAMESPACE_ENV}.",
            details={"path": str(overlay)},
        )
    return match.group("default").strip()


class ClerkMount(StrictRecord):
    """One service on a host that mounts a clerk's attested volume, and where."""

    service: str
    container: str
    target: str


class HostClerkFacts(StrictRecord):
    """Where one host's topology mounts one live clerk's volume."""

    clerk_id: str
    mounts: tuple[ClerkMount, ...]


class HostTopologyFacts(StrictRecord):
    """What one host's topology says about every live clerk in the registry.

    Recorded by export on the old host and read again by import on the new
    one; the re-approval check compares the two (#2269).
    """

    deployment_namespace: str
    clerks: tuple[HostClerkFacts, ...]


def host_topology_facts(
    registry: RegistryFacts, topology: Mapping[str, Any], *, namespace: str
) -> HostTopologyFacts:
    """This host's namespace and, per live clerk, every service mounting its volume."""
    volume_names = {
        key: str(spec.get("name") or key) for key, spec in topology["volumes"].items()
    }
    clerks: list[HostClerkFacts] = []
    for clerk in registry.clerks:
        if clerk.lifecycle_state == "retired":
            continue
        mounts = sorted(
            (
                ClerkMount(
                    service=service_name,
                    container=str(service.get("container_name") or service_name),
                    target=target,
                )
                for service_name, service in topology["service_detail"].items()
                for source, target, kind in _mounts(service)
                if kind == "volume" and volume_names.get(source) == clerk.attestation_id
            ),
            key=lambda mount: (mount.service, mount.target),
        )
        clerks.append(HostClerkFacts(clerk_id=clerk.clerk_id, mounts=tuple(mounts)))
    return HostTopologyFacts(deployment_namespace=namespace, clerks=tuple(clerks))


def _describe_mounts(mounts: Iterable[ClerkMount]) -> str:
    listed = [f"{mount.service} at {mount.target}" for mount in mounts]
    return ", ".join(listed) or "no service"


def reapproval_report(
    registry: RegistryFacts, source: HostTopologyFacts, destination: HostTopologyFacts
) -> list[dict[str, Any]]:
    """Whether each live clerk's host facts changed between the old host and this one.

    The registry travels unchanged, so what a clerk's recorded
    ``volume_root`` and approved endpoint mean can only change with the host:
    the namespace it registers clerks under, and which service mounts each
    clerk's volume where (the endpoint names that service). Only a
    difference between the old host's facts and this host's is
    ``reapproval_required`` — named, never fixed: re-approval is the
    operator's host ceremony. A registry value that already disagreed with
    the old host's topology (the dev Paper lane's ``/paper-volume``) is
    carried over as it was, not re-judged here.
    """
    endpoints = {row.clerk_id: row for row in registry.approved_endpoints}
    before = {entry.clerk_id: entry for entry in source.clerks}
    after = {entry.clerk_id: entry for entry in destination.clerks}
    report: list[dict[str, Any]] = []
    for clerk in registry.clerks:
        if clerk.lifecycle_state == "retired":
            continue
        issues: list[str] = []
        if source.deployment_namespace != destination.deployment_namespace:
            issues.append(
                f"the old host registered clerks under {source.deployment_namespace!r}; "
                f"this host registers under {destination.deployment_namespace!r}"
            )
        old, new = before.get(clerk.clerk_id), after.get(clerk.clerk_id)
        if old is None:
            issues.append("the bundle records no old-host mounts for this clerk")
        elif new is None or old.mounts != new.mounts:
            issues.append(
                f"volume {clerk.attestation_id!r} was mounted by "
                f"{_describe_mounts(old.mounts)} on the old host and by "
                f"{_describe_mounts(new.mounts if new else ())} here"
            )
        endpoint = endpoints.get(clerk.clerk_id)
        report.append(
            {
                "clerk_id": clerk.clerk_id,
                "volume_root": clerk.volume_root,
                "endpoint_ref": None if endpoint is None else endpoint.endpoint_ref,
                "base_url": None if endpoint is None else endpoint.base_url,
                "reapproval_required": bool(issues),
                "issues": issues,
            }
        )
    return report


__all__ = [
    "ClerkMount",
    "HostClerkFacts",
    "HostTopologyFacts",
    "compose_variable",
    "containers_writing_folders",
    "deployment_namespace",
    "host_topology_facts",
    "load_topology",
    "postgres_image_major",
    "reapproval_report",
    "required_env_files",
    "required_fleet_env_files",
]
