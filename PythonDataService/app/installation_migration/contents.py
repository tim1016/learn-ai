"""What an installation bundle carries, and what it must never carry (#2268).

Owner decision (grill session 2026-09-22, #2151 comment): the bundle holds the
five podman volumes, ``data-lake-volume/``, the whole
``PythonDataService/artifacts/`` and ``PythonDataService/lean-cache`` — the
rule being that every gitignored folder a container mounts goes in, which by
that same rule carries ``PythonDataService/cache`` too — and no secret at all.

The bundle covers the **dev topology only**: ``compose.yaml`` +
``compose.fleet.dev.yaml``, the stack the owner runs. Another topology (the
production fleet overlay, a Windows host) needs its own review of this list.

The list is declared here, once, rather than re-derived from the Compose
files at run time: a migration must move what the operator reviewed, not
whatever a local override happens to mount. ``tests/installation_migration/
test_contents.py`` pins it against the committed topology in both
directions, so a new mounted volume or gitignored bind fails the build until
it is either bundled or excluded here with its reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.installation_migration.topology import compose_variable
from app.lean_sidecar.launcher_auth import LAUNCHER_TOKEN_FILENAME

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
    BundledFolder("PythonDataService/cache"),
    BundledFolder("PythonDataService/lean-cache"),
)

#: Out-of-repo folders a container mounts that the bundle deliberately does
#: not carry, each with the reason the owner's rule (every *gitignored*
#: folder a container mounts goes in) does not reach it.
EXCLUDED_BIND_MOUNTS: dict[str, str] = {
    "../Lean/Data": (
        "Outside the repository: the LEAN reference data a separate checkout "
        "provides, mounted read-only."
    ),
}

#: The secret-shaped names no bundled folder may contain. The operator copies
#: ``deploy/fleet/env/*.env`` (and the other ``.env`` files) by hand; every
#: token a process mints under ``artifacts/`` is minted afresh on the new
#: host. ``LAUNCHER_TOKEN_FILENAME`` is the LEAN launcher's live token, named
#: by its canonical definition. The retired host daemon's
#: ``.host-daemon-token`` (ADR 0007) and the clerk host-binding capability
#: have no canonical definition left in code, so they are caught by shape:
#: any ``*-token``/``*_token`` name, any hidden name mentioning a token or a
#: capability, key material (``*.pem``, ``*.key``, SSH ``id_*`` keys, and
#: ``*.p12``/``*.pfx``/``*.jks`` keystores), and any name mentioning a
#: secret, password or credential.
_SECRET_FILE_NAMES = frozenset(
    {".env", "compose.override.yaml", "compose.override.yml", LAUNCHER_TOKEN_FILENAME}
)
_SECRET_SUFFIXES = (".env", "-token", "_token", ".pem", ".key", ".p12", ".pfx", ".jks")
_SECRET_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
_SECRET_WORDS = ("secret", "password", "credential")
_HIDDEN_SECRET_WORDS = ("token", "capability")


def is_secret_shaped(name: str) -> bool:
    """Whether a file name is one the bundle must never carry."""
    lowered = name.lower()
    return (
        name in _SECRET_FILE_NAMES
        or lowered.endswith(_SECRET_SUFFIXES)
        or lowered.startswith(_SECRET_PREFIXES)
        or any(word in lowered for word in _SECRET_WORDS)
        or (name.startswith(".") and any(word in lowered for word in _HIDDEN_SECRET_WORDS))
    )


def resolve_folder_path(
    repo_root: Path, folder: BundledFolder, *, lake_dir: Path | None
) -> Path:
    """The host path of one bundled folder on this machine.

    The lake follows an explicit ``lake_dir`` first, then
    ``LEAN_DATA_VOLUME_HOST_PATH`` as Compose resolves it for the bind (the
    process environment, then the repo-root ``.env``), then the repo-relative
    default — the same order the bind resolves in.
    """
    if folder.key == LAKE_FOLDER_KEY:
        if lake_dir is not None:
            return lake_dir
        override = compose_variable(repo_root, LAKE_HOST_PATH_ENV)
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
    "is_secret_shaped",
    "resolve_folder_path",
]
