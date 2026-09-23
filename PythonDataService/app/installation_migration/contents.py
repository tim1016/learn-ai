"""What an installation bundle carries, and what it must never carry (#2268).

Owner decision (grill session 2026-09-22, #2151 comment): the bundle holds the
five podman volumes, ``data-lake-volume/``, the whole
``PythonDataService/artifacts/`` and ``PythonDataService/lean-cache`` — the
rule being that every gitignored folder a container mounts goes in — and no
secret at all.

The list is declared here, once, rather than re-derived from the Compose
files at run time: a migration must move what the operator reviewed, not
whatever a local override happens to mount. ``tests/installation_migration/
test_contents.py`` pins it against the committed topology in both
directions, so a new mounted volume or gitignored bind fails the build until
it is either bundled or excluded here with its reason.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

VolumeRole = Literal["postgres", "fleet_control", "clerk", "qualification"]


@dataclass(frozen=True, slots=True)
class BundledVolume:
    """One podman named volume the bundle carries."""

    compose_key: str
    name: str
    role: VolumeRole

    @property
    def member(self) -> str:
        """Where this volume's export lives inside the bundle."""
        return f"volumes/{self.name}.tar"


@dataclass(frozen=True, slots=True)
class BundledFolder:
    """One host folder the bundle carries, keyed by its repo-relative path."""

    key: str

    @property
    def member(self) -> str:
        """Where this folder's tar lives inside the bundle."""
        return f"folders/{self.key.replace('/', '__')}.tar"


BUNDLED_VOLUMES: tuple[BundledVolume, ...] = (
    BundledVolume("pgdata", "learn-ai_pgdata", "postgres"),
    BundledVolume("alpaca-fleet-control", "learn-ai_alpaca-fleet-control", "fleet_control"),
    BundledVolume("alpaca-clerk-data", "learn-ai-alpaca-clerk-data", "clerk"),
    BundledVolume("alpaca-paper-clerk-data", "learn-ai-alpaca-paper-clerk-data", "clerk"),
    BundledVolume(
        "alpaca-clerk-qualification-data",
        "learn-ai-alpaca-clerk-qualification-data",
        "qualification",
    ),
)

#: The lake's folder key. Its host path follows ``LEAN_DATA_VOLUME_HOST_PATH``
#: exactly as the Compose bind does.
LAKE_FOLDER_KEY = "data-lake-volume"
LAKE_HOST_PATH_ENV = "LEAN_DATA_VOLUME_HOST_PATH"

BUNDLED_FOLDERS: tuple[BundledFolder, ...] = (
    BundledFolder(LAKE_FOLDER_KEY),
    BundledFolder("PythonDataService/artifacts"),
    BundledFolder("PythonDataService/lean-cache"),
)

#: Gitignored or out-of-repo folders a container mounts that the bundle
#: deliberately does not carry, each with the reason the owner's rule does
#: not reach it.
EXCLUDED_BIND_MOUNTS: dict[str, str] = {
    "PythonDataService/cache": (
        "Derived analytics cache, regenerated from Polygon data (.gitignore); "
        "not in the owner-approved bundle list."
    ),
    "../Lean/Data": (
        "Outside the repository: the LEAN reference data a separate checkout "
        "provides, mounted read-only."
    ),
}

#: The secret-shaped names no bundled folder may contain. The operator copies
#: ``deploy/fleet/env/*.env`` (and the other ``.env`` files) by hand.
_SECRET_FILE_NAMES = frozenset({".env", "compose.override.yaml", "compose.override.yml"})


def is_secret_shaped(name: str) -> bool:
    """Whether a file name is one the bundle must never carry."""
    return name in _SECRET_FILE_NAMES or name.endswith(".env")


def find_secret_files(root: Path) -> Iterator[Path]:
    """Every secret-shaped file under ``root``, never following a symlink."""
    for directory, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            if is_secret_shaped(name):
                yield Path(directory) / name


def resolve_folder_path(
    repo_root: Path, folder: BundledFolder, *, lake_dir: Path | None
) -> Path:
    """The host path of one bundled folder on this machine.

    The lake follows an explicit ``lake_dir`` first, then
    ``LEAN_DATA_VOLUME_HOST_PATH`` (the variable the Compose bind reads),
    then the repo-relative default — the same order the bind resolves in.
    """
    if folder.key == LAKE_FOLDER_KEY:
        if lake_dir is not None:
            return lake_dir
        override = os.environ.get(LAKE_HOST_PATH_ENV, "").strip()
        if override:
            candidate = Path(override)
            return candidate if candidate.is_absolute() else repo_root / candidate
    return repo_root / folder.key


__all__ = [
    "BUNDLED_FOLDERS",
    "BUNDLED_VOLUMES",
    "EXCLUDED_BIND_MOUNTS",
    "LAKE_FOLDER_KEY",
    "LAKE_HOST_PATH_ENV",
    "BundledFolder",
    "BundledVolume",
    "find_secret_files",
    "is_secret_shaped",
    "resolve_folder_path",
]
