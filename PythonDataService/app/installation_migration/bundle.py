"""The single-file installation bundle and its manifest (#2268).

Format — one uncompressed tar, written under a ``.partial`` name and renamed
into place only once complete, never over an existing file::

    manifest.json                         first member, always
    volumes/<volume name>.tar             ``podman volume export`` output
    folders/<repo/relative/path>.tar      one tar per host folder
                                          (``/`` spelled ``__``)

The manifest names every member with its byte size, SHA-256 (tamper and
corruption evidence for the member as shipped) and content digest (the
canonical tree digest the destination is re-verified against after restore),
plus the identity facts import checks before anything is restored: registry
identity, clerks, assignment and authority generations, volume markers, and
the source git commit. Every instant is ``int64 ms UTC``.

The manifest is untrusted input on import. It is parsed **once** into the
strict, closed :class:`Manifest` model — every field present and of its exact
type, no field unknown — and every member name must be the one
:mod:`app.installation_migration.contents` defines for that volume or folder,
so no manifest string ever chooses where a byte lands. A malformed manifest is
a :class:`MigrationRefused`, never a traceback.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from app.installation_migration.contents import (
    BUNDLED_FOLDERS,
    BUNDLED_VOLUMES,
    VolumeRole,
)
from app.installation_migration.errors import MigrationRefused
from app.installation_migration.facts import (
    ClerkVolumeFacts,
    InstantMs,
    PostgresFacts,
    RegistryFacts,
    StrictRecord,
)

MANIFEST_MEMBER = "manifest.json"
#: 2 (#2269): the manifest names the secret-shaped files export skipped and
#: the source host's topology facts the re-approval check compares.
MANIFEST_SCHEMA_VERSION = 2
MANIFEST_KIND = "learn-ai-installation-bundle"

_CHUNK_BYTES = 1 << 20
_SHA256_HEX = r"^[0-9a-f]{64}$"
_GIT_COMMIT = r"^[0-9a-f]{40}$"


class VolumeEntry(StrictRecord):
    """One bundled podman volume."""

    name: str
    compose_key: str
    role: VolumeRole
    driver: str
    labels: dict[str, str]
    member: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=_SHA256_HEX)
    content_digest: str = Field(pattern=_SHA256_HEX)


class FolderEntry(StrictRecord):
    """One bundled host folder."""

    key: str
    member: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=_SHA256_HEX)
    content_digest: str = Field(pattern=_SHA256_HEX)


class SkippedSecretFile(StrictRecord):
    """One secret-shaped file export left out, and what it asks of the operator."""

    path: str
    note: str


class LaneEntry(StrictRecord):
    """One lane's quiet observation and stop receipt at export."""

    clerk_id: str
    broker: str
    account_id: str
    quiet_observed_at_ms: InstantMs
    stop_receipt_id: str


class Manifest(StrictRecord):
    """The whole manifest, parsed once and trusted only after this validates."""

    kind: Literal["learn-ai-installation-bundle"]
    manifest_schema_version: Literal[2]
    created_at_ms: InstantMs
    source_commit: str = Field(pattern=_GIT_COMMIT)
    source_tree_dirty: bool
    dirty_tree_override: bool
    operator: str
    change_ref: str
    volumes: tuple[VolumeEntry, ...]
    folders: tuple[FolderEntry, ...]
    registry: RegistryFacts
    clerk_volumes: tuple[ClerkVolumeFacts, ...]
    lanes: tuple[LaneEntry, ...]
    postgres: PostgresFacts
    skipped_secret_files: tuple[SkippedSecretFile, ...]

    @model_validator(mode="after")
    def _layout_is_this_builds(self) -> Manifest:
        """Exactly the bundled volumes and folders, each at its defined member."""
        expected_volumes = sorted(
            (volume.name, volume.compose_key, volume.role, volume.member)
            for volume in BUNDLED_VOLUMES
        )
        declared_volumes = sorted(
            (entry.name, entry.compose_key, entry.role, entry.member) for entry in self.volumes
        )
        if declared_volumes != expected_volumes:
            raise ValueError(
                f"the manifest declares volumes {declared_volumes}; this build bundles "
                f"exactly {expected_volumes}"
            )
        expected_folders = sorted((folder.key, folder.member) for folder in BUNDLED_FOLDERS)
        declared_folders = sorted((entry.key, entry.member) for entry in self.folders)
        if declared_folders != expected_folders:
            raise ValueError(
                f"the manifest declares folders {declared_folders}; this build bundles "
                f"exactly {expected_folders}"
            )
        return self

    def volume(self, name: str) -> VolumeEntry:
        """The entry of one bundled volume (the layout validator guarantees it)."""
        return next(entry for entry in self.volumes if entry.name == name)

    def folder(self, key: str) -> FolderEntry:
        """The entry of one bundled folder (the layout validator guarantees it)."""
        return next(entry for entry in self.folders if entry.key == key)

    def member_entries(self) -> dict[str, VolumeEntry | FolderEntry]:
        """Every payload member, keyed by the member name ``contents`` defines."""
        return {
            **{volume.member: self.volume(volume.name) for volume in BUNDLED_VOLUMES},
            **{folder.member: self.folder(folder.key) for folder in BUNDLED_FOLDERS},
        }


def _manifest_invalid(bundle: Path, exc: ValidationError) -> MigrationRefused:
    errors = [
        {"field": ".".join(str(part) for part in error["loc"]) or "<manifest>", "problem": error["msg"]}
        for error in exc.errors()
    ]
    listing = "; ".join(f"{error['field']}: {error['problem']}" for error in errors[:5])
    return MigrationRefused(
        "bundle_manifest_invalid",
        f"The manifest of {bundle} is not one this build can trust: {listing}.",
        details={"bundle": str(bundle), "errors": errors},
    )


def parse_manifest(payload: bytes, *, bundle: Path) -> Manifest:
    """Parse manifest bytes into a :class:`Manifest`, or refuse by reason."""
    try:
        loose = json.loads(payload.decode("utf-8"))
    except ValueError as exc:
        raise MigrationRefused(
            "bundle_manifest_invalid",
            f"The manifest of {bundle} is not JSON: {exc}",
            details={"bundle": str(bundle), "errors": [{"field": "<manifest>", "problem": str(exc)}]},
        ) from exc
    if isinstance(loose, dict) and loose.get("kind") == MANIFEST_KIND:
        version = loose.get("manifest_schema_version")
        if type(version) is int and version != MANIFEST_SCHEMA_VERSION:
            raise MigrationRefused(
                "bundle_manifest_unsupported",
                f"{bundle} has manifest schema {version}; this build reads "
                f"{MANIFEST_SCHEMA_VERSION}.",
                details={"bundle": str(bundle)},
            )
    try:
        return Manifest.model_validate_json(payload)
    except ValidationError as exc:
        raise _manifest_invalid(bundle, exc) from exc


def write_bundle(
    bundle: Path, manifest: Manifest, members: Sequence[tuple[str, Path]]
) -> None:
    """Write the bundle atomically; refuse to overwrite anything."""
    if bundle.exists():
        raise MigrationRefused(
            "bundle_exists",
            f"{bundle} already exists; a bundle is never overwritten.",
            details={"bundle": str(bundle)},
        )
    partial = bundle.with_name(f"{bundle.name}.partial")
    payload = manifest.model_dump_json(indent=2).encode("utf-8")
    try:
        # The write handle outlives the archive so the fsync covers the
        # end-of-archive blocks tarfile writes on close, and is taken on the
        # descriptor that wrote them — fsync on a read-only descriptor is not
        # a durability guarantee POSIX makes.
        with partial.open("xb") as handle:
            with tarfile.open(fileobj=handle, mode="w", format=tarfile.PAX_FORMAT) as archive:
                info = tarfile.TarInfo(MANIFEST_MEMBER)
                info.size = len(payload)
                info.mtime = manifest.created_at_ms // 1000
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(payload))
                for name, source in members:
                    archive.add(source, arcname=name, recursive=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, bundle)
        _fsync_directory(bundle.parent)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    """Make a rename into ``directory`` durable."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_manifest(bundle: Path) -> Manifest:
    """The bundle's manifest, which must be its first member."""
    if not bundle.is_file():
        raise MigrationRefused(
            "bundle_missing", f"No bundle file at {bundle}.", details={"bundle": str(bundle)}
        )
    try:
        with tarfile.open(bundle, "r:") as archive:
            first = archive.next()
            if first is None or first.name != MANIFEST_MEMBER or not first.isreg():
                raise MigrationRefused(
                    "bundle_manifest_missing",
                    f"{bundle} does not begin with {MANIFEST_MEMBER}.",
                    details={"bundle": str(bundle)},
                )
            handle = archive.extractfile(first)
            assert handle is not None  # a regular member always has content
            payload = handle.read()
    except (tarfile.TarError, OSError) as exc:
        raise MigrationRefused(
            "bundle_unreadable",
            f"{bundle} is not a readable installation bundle: {exc}",
            details={"bundle": str(bundle)},
        ) from exc
    return parse_manifest(payload, bundle=bundle)


def extract_verified_members(bundle: Path, manifest: Manifest, staging: Path) -> dict[str, Path]:
    """Stage every payload member, verifying each one's SHA-256 as it streams.

    The members expected are the ones :mod:`contents` defines, never names
    read from the manifest or the tar. A member outside that set, a defined
    member the bundle lacks, or a member whose bytes disagree with the
    manifest refuses — the staged copy of a mismatched member is removed
    with it, and nothing is ever restored from a bundle that failed here.
    """
    declared = manifest.member_entries()
    root = staging.resolve()
    staged: dict[str, Path] = {}
    try:
        with tarfile.open(bundle, "r:") as archive:
            for member in archive:
                if member.name == MANIFEST_MEMBER:
                    continue
                entry = declared.get(member.name)
                if entry is None or not member.isreg() or member.name in staged:
                    raise MigrationRefused(
                        "bundle_member_unexpected",
                        f"{bundle} carries {member.name!r}, which is not a member this "
                        "build defines exactly once.",
                        details={"bundle": str(bundle), "member": member.name},
                    )
                target = root / member.name
                if not target.resolve().is_relative_to(root):
                    raise MigrationRefused(
                        "bundle_member_unsafe",
                        f"{member.name!r} in {bundle} would land outside the staging area.",
                        details={"bundle": str(bundle), "member": member.name},
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                assert source is not None  # isreg() above
                digest = hashlib.sha256()
                with source, target.open("xb") as sink:
                    while chunk := source.read(_CHUNK_BYTES):
                        digest.update(chunk)
                        sink.write(chunk)
                if digest.hexdigest() != entry.sha256:
                    target.unlink()
                    raise MigrationRefused(
                        "bundle_member_hash_mismatch",
                        f"{member.name} in {bundle} does not match the SHA-256 its "
                        "manifest records; the bundle was altered or corrupted.",
                        details={
                            "bundle": str(bundle),
                            "member": member.name,
                            "expected_sha256": entry.sha256,
                            "actual_sha256": digest.hexdigest(),
                        },
                    )
                staged[member.name] = target
    except tarfile.TarError as exc:
        raise MigrationRefused(
            "bundle_unreadable",
            f"{bundle} could not be read to the end: {exc}",
            details={"bundle": str(bundle)},
        ) from exc
    missing = sorted(set(declared) - set(staged))
    if missing:
        raise MigrationRefused(
            "bundle_member_missing",
            f"{bundle} lacks {', '.join(missing)}, which its manifest declares.",
            details={"bundle": str(bundle), "members": missing},
        )
    return staged


__all__ = [
    "MANIFEST_KIND",
    "MANIFEST_MEMBER",
    "MANIFEST_SCHEMA_VERSION",
    "FolderEntry",
    "LaneEntry",
    "Manifest",
    "SkippedSecretFile",
    "VolumeEntry",
    "extract_verified_members",
    "parse_manifest",
    "read_manifest",
    "write_bundle",
]
