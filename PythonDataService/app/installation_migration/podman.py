"""The podman seam of installation migration (#2268).

Everything the migration asks of podman goes through :class:`PodmanPort`, so
the export/import logic is exercised against a fake in tests and against
:class:`SubprocessPodman` on a real host. The real adapter runs ``podman``
with an argument list, never a shell, and with a timeout on every call: the
Windows follow-up (#2269) reuses it unchanged.

A failed command is a :class:`MigrationRefused` naming the volume or
container and carrying podman's own stderr — never a swallowed exit code.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.installation_migration.errors import MigrationRefused

#: Seconds a volume export/import may take; the bundle is ~1.1 GB today.
DEFAULT_TRANSFER_TIMEOUT_S = 1800.0
#: Seconds any other podman call may take.
DEFAULT_COMMAND_TIMEOUT_S = 120.0
#: Seconds ``podman stop`` gives a container before it kills it: long enough
#: for uvicorn's own 10 s graceful shutdown and a clean Postgres checkpoint.
STOP_GRACE_S = 60

#: ``podman system df --verbose`` prints sizes through go-units' decimal
#: ``HumanSize`` ("106.2MB", "24.58kB", "0B"); ``--format`` cannot be combined
#: with ``--verbose``, so the one per-volume size podman reports is this text.
_HUMAN_SIZE = re.compile(r"^(?P<number>\d+(?:\.\d+)?)(?P<unit>[kMGTP]?B)$")
_DECIMAL_UNITS = {"B": 1, "kB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12, "PB": 10**15}


@dataclass(frozen=True, slots=True)
class VolumeInfo:
    """What the migration records about one named volume."""

    name: str
    driver: str
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContainerUse:
    """One container that references a volume, and whether it is running."""

    name: str
    running: bool


class PodmanPort(Protocol):
    """The podman operations installation migration needs."""

    def volume_info(self, name: str) -> VolumeInfo | None: ...

    def export_volume(self, name: str, destination: Path) -> None: ...

    def create_volume(self, info: VolumeInfo) -> None: ...

    def import_volume(self, name: str, source: Path) -> None: ...

    def remove_volume(self, name: str) -> None: ...

    def volume_size_bytes(self, name: str) -> int | None: ...

    def containers_using_volume(self, name: str) -> list[ContainerUse]: ...

    def container_running(self, name: str) -> bool | None: ...

    def stop_container(self, name: str) -> None: ...


Runner = Callable[..., subprocess.CompletedProcess[str]]


class SubprocessPodman:
    """:class:`PodmanPort` over the ``podman`` executable."""

    def __init__(
        self,
        *,
        executable: str = "podman",
        run: Runner = subprocess.run,
        transfer_timeout_s: float = DEFAULT_TRANSFER_TIMEOUT_S,
        command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
    ) -> None:
        self._executable = executable
        self._run = run
        self._transfer_timeout_s = transfer_timeout_s
        self._command_timeout_s = command_timeout_s

    def _call(
        self,
        arguments: Sequence[str],
        *,
        subject: str,
        timeout_s: float | None = None,
        allowed_exit_codes: frozenset[int] = frozenset({0}),
    ) -> subprocess.CompletedProcess[str]:
        command = [self._executable, *arguments]
        try:
            completed = self._run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_s or self._command_timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise MigrationRefused(
                "podman_command_timed_out",
                f"`{' '.join(command)}` did not finish within {exc.timeout} s ({subject}).",
                details={"command": command, "subject": subject},
            ) from exc
        except FileNotFoundError as exc:
            raise MigrationRefused(
                "podman_unavailable",
                f"The podman executable {self._executable!r} was not found.",
                details={"executable": self._executable},
            ) from exc
        if completed.returncode not in allowed_exit_codes:
            raise MigrationRefused(
                "podman_command_failed",
                f"`{' '.join(command)}` exited {completed.returncode} ({subject}): "
                f"{completed.stderr.strip()}",
                details={
                    "command": command,
                    "subject": subject,
                    "exit_code": completed.returncode,
                    "stderr": completed.stderr.strip(),
                },
            )
        return completed

    def volume_info(self, name: str) -> VolumeInfo | None:
        exists = self._call(
            ["volume", "exists", name], subject=name, allowed_exit_codes=frozenset({0, 1})
        )
        if exists.returncode == 1:
            return None
        inspected = self._call(["volume", "inspect", "--format", "json", name], subject=name)
        try:
            payload = json.loads(inspected.stdout)
            record = payload[0] if isinstance(payload, list) else payload
            return VolumeInfo(
                name=str(record["Name"]),
                driver=str(record.get("Driver") or "local"),
                labels={str(k): str(v) for k, v in (record.get("Labels") or {}).items()},
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise MigrationRefused(
                "podman_output_unreadable",
                f"`podman volume inspect {name}` answered something this tool cannot read.",
                details={"volume": name, "stdout": inspected.stdout[:2000]},
            ) from exc

    def export_volume(self, name: str, destination: Path) -> None:
        self._call(
            ["volume", "export", "--output", str(destination), name],
            subject=name,
            timeout_s=self._transfer_timeout_s,
        )

    def create_volume(self, info: VolumeInfo) -> None:
        arguments = ["volume", "create", "--driver", info.driver]
        for key in sorted(info.labels):
            arguments.extend(["--label", f"{key}={info.labels[key]}"])
        arguments.append(info.name)
        self._call(arguments, subject=info.name)

    def import_volume(self, name: str, source: Path) -> None:
        self._call(
            ["volume", "import", name, str(source)],
            subject=name,
            timeout_s=self._transfer_timeout_s,
        )

    def volume_size_bytes(self, name: str) -> int | None:
        """The volume's size as podman reports it, or ``None`` when it does not.

        ``None`` is "unknown", never zero: the caller estimates instead.
        """
        listed = self._call(["system", "df", "--verbose"], subject=name)
        in_volumes = False
        for line in listed.stdout.splitlines():
            if line.startswith("Local Volumes space usage"):
                in_volumes = True
                continue
            if in_volumes and line.endswith("space usage:"):
                break
            fields = line.split()
            if in_volumes and len(fields) == 3 and fields[0] == name:
                match = _HUMAN_SIZE.match(fields[2])
                if match is None:
                    return None
                return round(float(match["number"]) * _DECIMAL_UNITS[match["unit"]])
        return None

    def remove_volume(self, name: str) -> None:
        # Never --force: a volume a container still references must refuse,
        # not take the container with it.
        self._call(["volume", "rm", name], subject=name)

    def containers_using_volume(self, name: str) -> list[ContainerUse]:
        listed = self._call(
            ["ps", "--all", "--filter", f"volume={name}", "--format", "json"], subject=name
        )
        try:
            rows = json.loads(listed.stdout or "[]") or []
            return [
                ContainerUse(
                    name=str((row.get("Names") or [row.get("Id")])[0]),
                    running=str(row.get("State", "")).lower() == "running",
                )
                for row in rows
            ]
        except (ValueError, TypeError, IndexError, AttributeError) as exc:
            raise MigrationRefused(
                "podman_output_unreadable",
                f"`podman ps --filter volume={name}` answered something this tool cannot read.",
                details={"volume": name, "stdout": listed.stdout[:2000]},
            ) from exc

    def container_running(self, name: str) -> bool | None:
        exists = self._call(
            ["container", "exists", name], subject=name, allowed_exit_codes=frozenset({0, 1})
        )
        if exists.returncode == 1:
            return None
        state = self._call(
            ["container", "inspect", "--format", "{{.State.Running}}", name], subject=name
        )
        return state.stdout.strip().lower() == "true"

    def stop_container(self, name: str) -> None:
        self._call(
            ["stop", "--time", str(STOP_GRACE_S), name],
            subject=name,
            timeout_s=STOP_GRACE_S + self._command_timeout_s,
        )


__all__ = [
    "ContainerUse",
    "PodmanPort",
    "SubprocessPodman",
    "VolumeInfo",
]
