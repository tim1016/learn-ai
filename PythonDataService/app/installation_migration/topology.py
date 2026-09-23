"""What the committed fleet topology tells a migration about a host (#2268).

Read from the checkout's own ``deploy/fleet/topology.snapshot.json`` — the
rendered, secret-free projection of ``compose.yaml`` + ``compose.fleet.dev.yaml``
— so the old host and the new one each answer from the code they run:

- which containers write a bundled host folder (they are stopped before the
  copy, and must be stopped before a restore);
- which ``deploy/fleet/env/*.env`` files the stack cannot start without
  (import refuses until the operator has copied them by hand);
- whether each registry clerk's recorded ``volume_root`` and approved
  endpoint still resolve here, and so whether a re-approval is needed —
  reported, never performed.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from app.installation_migration.errors import MigrationRefused

if TYPE_CHECKING:
    from app.installation_migration.facts import RegistryFacts

TOPOLOGY_SNAPSHOT = Path("deploy") / "fleet" / "topology.snapshot.json"
FLEET_OVERLAY = Path("compose.fleet.dev.yaml")
FLEET_ENV_DIRECTORY = Path("deploy") / "fleet" / "env"
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


def deployment_namespace(repo_root: Path) -> str:
    """The namespace the fleet overlay will register clerks under on this host.

    Resolved the way Compose resolves the overlay's
    ``${FLEET_DEPLOYMENT_NAMESPACE:-…}``: the process environment, then the
    repo-root ``.env`` Compose interpolates from, then the committed default.
    """
    from_environment = os.environ.get(NAMESPACE_ENV, "").strip()
    if from_environment:
        return from_environment
    dotenv = repo_root / ".env"
    if dotenv.is_file():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and key.strip() == NAMESPACE_ENV and value.strip():
                return value.strip().strip("'\"")
    overlay = repo_root / FLEET_OVERLAY
    match = _NAMESPACE_DEFAULT.search(overlay.read_text(encoding="utf-8"))
    if match is None:
        raise MigrationRefused(
            "topology_unreadable",
            f"{overlay} declares no default {NAMESPACE_ENV}.",
            details={"path": str(overlay)},
        )
    return match.group("default").strip()


def host_resolution_report(
    registry: RegistryFacts, topology: Mapping[str, Any], *, namespace: str
) -> list[dict[str, Any]]:
    """Whether each live clerk's volume root and endpoint resolve on this host.

    A clerk resolves when this host registers clerks under its recorded
    namespace, some service mounts the clerk's attested named volume at its
    recorded ``volume_root``, and its approved endpoint's host is that same
    service. Anything else is ``reapproval_required`` — named, never fixed:
    re-approval is the operator's host ceremony.
    """
    volume_names = {
        key: str(spec.get("name") or key) for key, spec in topology["volumes"].items()
    }
    endpoints = {row.clerk_id: row for row in registry.approved_endpoints}
    report: list[dict[str, Any]] = []
    for clerk in registry.clerks:
        if clerk.lifecycle_state == "retired":
            continue
        serving: dict[str, set[str]] = {}
        for service_name, service in topology["service_detail"].items():
            for source, target, kind in _mounts(service):
                if kind == "volume" and volume_names.get(source) == clerk.attestation_id:
                    names = {service_name, str(service.get("container_name") or service_name)}
                    serving.setdefault(target, set()).update(names)
        issues: list[str] = []
        if clerk.deployment_namespace != namespace:
            issues.append(
                f"registered under namespace {clerk.deployment_namespace!r}; this "
                f"host registers under {namespace!r}"
            )
        hosts = serving.get(clerk.volume_root)
        if hosts is None:
            issues.append(
                f"no service here mounts volume {clerk.attestation_id!r} at "
                f"{clerk.volume_root!r}"
            )
        endpoint = endpoints.get(clerk.clerk_id)
        if endpoint is not None:
            endpoint_host = urlsplit(endpoint.base_url).hostname
            if hosts is None or endpoint_host not in hosts:
                issues.append(
                    f"approved endpoint {endpoint.base_url!r} does not name the "
                    "service that mounts this clerk's volume here"
                )
        report.append(
            {
                "clerk_id": clerk.clerk_id,
                "volume_root": clerk.volume_root,
                "endpoint_ref": None if endpoint is None else endpoint.endpoint_ref,
                "base_url": None if endpoint is None else endpoint.base_url,
                "resolves": not issues,
                "reapproval_required": bool(issues),
                "issues": issues,
            }
        )
    return report


__all__ = [
    "containers_writing_folders",
    "deployment_namespace",
    "host_resolution_report",
    "load_topology",
    "required_fleet_env_files",
]
