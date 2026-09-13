"""Clerk volume identity: the marker, the canonical root, and the proof order.

PRD FR-020–027: every production clerk owns a distinct physical volume whose
root carries a versioned identity marker naming broker, clerk, volume and a
nonsecret mount attestation. Verification — canonical root, marker, deployment
expectation, registry identity — happens *before* any database writer or
broker client opens; a missing, copied, symlinked, noncanonical or mis-mounted
identity fails closed.

The marker file is plain JSON, deliberately not secret: anything that can read
the volume can read the marker. Copying a volume therefore cannot forge a new
identity — the copied marker still names the original clerk, and provisioning
against a root that already carries one refuses with ``clerk_volume_clone_detected``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from stat import S_ISDIR, S_ISLNK

from app.broker.fleet.errors import (
    ClerkVolumeCloneDetected,
    ClerkVolumeIdentityMismatch,
    ClerkVolumeIdentityMissing,
    ClerkVolumeMountUnproven,
)
from app.broker.fleet.records import VolumeMarker

MARKER_FILENAME = ".learn-ai-clerk-volume.json"
MARKER_SCHEMA_VERSION = 1

_MARKER_FIELDS = (
    "marker_version",
    "broker",
    "clerk_id",
    "volume_id",
    "attestation_kind",
    "attestation_id",
    "created_at_ms",
)


def marker_path(root: Path) -> Path:
    return root / MARKER_FILENAME


def resolve_canonical_root(root: Path) -> Path:
    """Prove ``root`` is a real directory reached without a symlink anywhere.

    A symlinked component is a mis-mount wearing a canonical name (a bind or
    a link into another volume), so it refuses rather than resolving through
    (PRD FR-026). ``OSError`` and a missing root refuse the same way: an
    unavailable volume is not permission to proceed.
    """
    try:
        probe = root
        # Walk outward checking every component; a dangling parent is an
        # unavailable installation, not a fresh one.
        while True:
            metadata = probe.lstat()
            if S_ISLNK(metadata.st_mode):
                raise ClerkVolumeMountUnproven(
                    f"The clerk volume path {root} reaches through the symlink {probe}; "
                    "a canonical mounted volume root is required.",
                    next_step="Mount the named volume directly at the configured root and retry.",
                )
            if probe == probe.parent:
                break
            probe = probe.parent
        metadata = root.lstat()
    except FileNotFoundError as exc:
        raise ClerkVolumeMountUnproven(
            f"The clerk volume root {root} does not exist.",
            next_step="Mount the clerk's named volume at the configured root and retry.",
        ) from exc
    except OSError as exc:
        raise ClerkVolumeMountUnproven(
            f"The clerk volume root {root} could not be inspected: {exc}",
            next_step="Restore access to the clerk's named volume and retry.",
        ) from exc
    if not S_ISDIR(metadata.st_mode):
        raise ClerkVolumeMountUnproven(
            f"The clerk volume root {root} is not a directory.",
            next_step="Point the clerk at its mounted volume root and retry.",
        )
    return root


def write_volume_marker(root: Path, marker: VolumeMarker) -> None:
    """Write the marker atomically; refuse to overwrite any existing identity.

    A root that already carries a marker is not a fresh volume. The common
    cause is a copied or re-mounted clone, so the refusal names that family
    (PRD FR-026).
    """
    target = marker_path(resolve_canonical_root(root))
    if target.exists():
        raise ClerkVolumeCloneDetected(
            f"The volume root {root} already carries an identity marker; a clerk "
            "volume is provisioned once and never re-marked in place.",
            next_step="Use a fresh named volume for a new clerk, or re-verify the "
            "existing one against its registry identity.",
        )
    payload = json.dumps(
        {field: getattr(marker, field) for field in _MARKER_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
    )
    # delete=False: the tmp file is renamed into place below, not closed-and-
    # discarded — the same deliberate exception ``advisory_lock`` documents.
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115
        "w", encoding="utf-8", dir=root, prefix=".marker-", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, target)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def read_volume_marker(root: Path) -> VolumeMarker | None:
    """Parse the marker if present; ``None`` only means "no marker here"."""
    target = marker_path(root)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ClerkVolumeIdentityMismatch(
            f"The identity marker at {target} is unreadable: {exc}",
            next_step="Restore the volume from backup or re-provision the clerk.",
        ) from exc
    if not isinstance(raw, dict) or set(raw) != set(_MARKER_FIELDS):
        raise ClerkVolumeIdentityMismatch(
            f"The identity marker at {target} does not match marker schema "
            f"version {MARKER_SCHEMA_VERSION}.",
            next_step="Re-provision the clerk volume with this build.",
        )
    marker = VolumeMarker(
        marker_version=int(raw["marker_version"]),
        broker=str(raw["broker"]),
        clerk_id=str(raw["clerk_id"]),
        volume_id=str(raw["volume_id"]),
        attestation_kind=str(raw["attestation_kind"]),
        attestation_id=str(raw["attestation_id"]),
        created_at_ms=int(raw["created_at_ms"]),
    )
    if marker.marker_version != MARKER_SCHEMA_VERSION:
        raise ClerkVolumeIdentityMismatch(
            f"The identity marker at {target} is marker_version="
            f"{marker.marker_version}; this build reads {MARKER_SCHEMA_VERSION}.",
            next_step="Run the build that provisioned this volume, or re-provision it.",
        )
    return marker


def verify_volume_identity(
    root: Path,
    *,
    expected_broker: str,
    expected_clerk_id: str,
    expected_volume_id: str,
    expected_attestation_kind: str,
    expected_attestation_id: str,
) -> VolumeMarker:
    """The fail-before-authority gate (PRD FR-025): prove the mounted root.

    Order matters and is observable through the refusal family: canonical
    mount first, marker presence second, marker agreement last. A caller that
    passes this has proven the root, the marker and the registry expectation
    agree, and may open writers.
    """
    resolve_canonical_root(root)
    marker = read_volume_marker(root)
    if marker is None:
        raise ClerkVolumeIdentityMissing(
            f"The volume root {root} carries no identity marker; a clerk volume "
            "is provisioned with one before any authority opens.",
            next_step="Provision the clerk (which writes the marker) or restore the volume.",
        )
    mismatches = [
        (field, getattr(marker, field), expected)
        for field, expected in (
            ("broker", expected_broker),
            ("clerk_id", expected_clerk_id),
            ("volume_id", expected_volume_id),
            ("attestation_kind", expected_attestation_kind),
            ("attestation_id", expected_attestation_id),
        )
        if getattr(marker, field) != expected
    ]
    if mismatches:
        detail = ", ".join(f"{field}={actual!r}" for field, actual, _ in mismatches)
        raise ClerkVolumeIdentityMismatch(
            f"The identity marker at {root} disagrees with the registry identity "
            f"({detail}); a copied or mis-mounted volume refuses before any "
            "writer opens.",
            next_step="Mount the clerk's own named volume at the configured root and retry.",
        )
    return marker


__all__ = [
    "MARKER_FILENAME",
    "MARKER_SCHEMA_VERSION",
    "marker_path",
    "read_volume_marker",
    "resolve_canonical_root",
    "verify_volume_identity",
    "write_volume_marker",
]
