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
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.installation_migration.errors import MigrationRefused

MANIFEST_MEMBER = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_KIND = "learn-ai-installation-bundle"

_CHUNK_BYTES = 1 << 20
_REQUIRED_KEYS: Mapping[str, type] = {
    "kind": str,
    "manifest_schema_version": int,
    "created_at_ms": int,
    "source_commit": str,
    "source_tree_dirty": bool,
    "operator": str,
    "change_ref": str,
    "volumes": list,
    "folders": list,
    "registry": dict,
    "clerk_volumes": list,
    "lanes": list,
}


def validate_manifest(manifest: Mapping[str, Any], *, bundle: Path) -> dict[str, Any]:
    """Refuse a manifest this build cannot trust as a complete description."""
    for key, expected in _REQUIRED_KEYS.items():
        value = manifest.get(key)
        if isinstance(value, bool) and expected is int:
            value = None
        if not isinstance(value, expected):
            raise MigrationRefused(
                "bundle_manifest_invalid",
                f"The manifest of {bundle} has no valid {key!r}.",
                details={"bundle": str(bundle), "key": key},
            )
    if manifest["kind"] != MANIFEST_KIND:
        raise MigrationRefused(
            "bundle_manifest_invalid",
            f"{bundle} is not an installation bundle (kind {manifest['kind']!r}).",
            details={"bundle": str(bundle)},
        )
    if manifest["manifest_schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise MigrationRefused(
            "bundle_manifest_unsupported",
            f"{bundle} has manifest schema {manifest['manifest_schema_version']}; this "
            f"build reads {MANIFEST_SCHEMA_VERSION}.",
            details={"bundle": str(bundle)},
        )
    return dict(manifest)


def manifest_members(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Every payload member the manifest declares, keyed by member name."""
    entries = [*manifest["volumes"], *manifest["folders"]]
    members = {str(entry["member"]): entry for entry in entries}
    if len(members) != len(entries):
        raise MigrationRefused(
            "bundle_manifest_invalid",
            "The manifest declares one bundle member twice.",
            details={},
        )
    return members


def write_bundle(
    bundle: Path, manifest: Mapping[str, Any], members: Sequence[tuple[str, Path]]
) -> None:
    """Write the bundle atomically; refuse to overwrite anything."""
    if bundle.exists():
        raise MigrationRefused(
            "bundle_exists",
            f"{bundle} already exists; a bundle is never overwritten.",
            details={"bundle": str(bundle)},
        )
    partial = bundle.with_name(f"{bundle.name}.partial")
    payload = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
    try:
        with tarfile.open(partial, "x", format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo(MANIFEST_MEMBER)
            info.size = len(payload)
            info.mtime = int(manifest["created_at_ms"]) // 1000
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(payload))
            for name, source in members:
                archive.add(source, arcname=name, recursive=False)
        with partial.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(partial, bundle)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def read_manifest(bundle: Path) -> dict[str, Any]:
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
            manifest = json.loads(handle.read().decode("utf-8"))
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise MigrationRefused(
            "bundle_unreadable",
            f"{bundle} is not a readable installation bundle: {exc}",
            details={"bundle": str(bundle)},
        ) from exc
    if not isinstance(manifest, dict):
        raise MigrationRefused(
            "bundle_manifest_invalid",
            f"The manifest of {bundle} is not an object.",
            details={"bundle": str(bundle)},
        )
    return validate_manifest(manifest, bundle=bundle)


def extract_verified_members(
    bundle: Path, manifest: Mapping[str, Any], staging: Path
) -> dict[str, Path]:
    """Stage every payload member, verifying each one's SHA-256 as it streams.

    A member the manifest does not declare, a declared member the bundle
    lacks, or a member whose bytes disagree with the manifest refuses — the
    staged copy of a mismatched member is removed with it, and nothing is
    ever restored from a bundle that failed here.
    """
    declared = manifest_members(manifest)
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
                        f"{bundle} carries {member.name!r}, which its manifest does not "
                        "declare exactly once.",
                        details={"bundle": str(bundle), "member": member.name},
                    )
                target = staging / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                assert source is not None  # isreg() above
                digest = hashlib.sha256()
                with source, target.open("xb") as sink:
                    while chunk := source.read(_CHUNK_BYTES):
                        digest.update(chunk)
                        sink.write(chunk)
                if digest.hexdigest() != entry["sha256"]:
                    target.unlink()
                    raise MigrationRefused(
                        "bundle_member_hash_mismatch",
                        f"{member.name} in {bundle} does not match the SHA-256 its "
                        "manifest records; the bundle was altered or corrupted.",
                        details={
                            "bundle": str(bundle),
                            "member": member.name,
                            "expected_sha256": entry["sha256"],
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
    "extract_verified_members",
    "manifest_members",
    "read_manifest",
    "validate_manifest",
    "write_bundle",
]
