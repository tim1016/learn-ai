"""Root identity — the portable, server-owned answer to "which physical lake
root is this."

Issue #1876 (PR A of #1861): the data lake catalog has exactly one implicit
physical root today. Before more than one can coexist, every root needs an
identity that survives being remounted at a different filesystem path — never
derived from the path itself (fixed design decision, #1876) — and every
consumer needs a way to prove the root it is about to read or write actually
is the one it thinks it is.

That proof is an immutable marker at ``<base-root>/lake/.data-root.json``::

    {"schema_version": 1, "data_root_id": "00000000-0000-0000-0000-000000000000"}

No timestamps, no host-specific paths — the marker's only job is to carry a
UUID across a remount. :func:`resolve_root_context` is the one function
normal application code calls: it reads the marker, compares it against the
caller's expected UUID, and raises :class:`LakeRootIdentityError` on
anything but an exact match. It never writes. Marker creation is atomic and
reachable only through the three administrative functions below
(:func:`init_empty_root`, :func:`stamp_existing_root`, :func:`inspect_root`) —
see ``scripts/manage_data_root.py`` for the CLI wrapping them. This asymmetry
is the whole safety property: an application that starts up against a wiped
or freshly-mounted root fails closed instead of quietly adopting it.

Sole authority for lake path construction stays ``app.data_lake.path_policy``
(its own module docstring says so) — :class:`RootContext`'s path methods
delegate there rather than re-deriving anything.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

# Re-exported: active_root_id()/LEGACY_ROOT_ID's canonical home is
# app.config, not here — see that module's comment on why. Every caller in
# this codebase still reaches them as `root_identity.active_root_id()` /
# `root_identity.LEGACY_ROOT_ID`, which is where the "root identity" concept
# actually belongs conceptually.
from app.config import LEGACY_ROOT_ID as LEGACY_ROOT_ID
from app.config import active_root_id as active_root_id
from app.config import settings
from app.data_lake import path_policy
from app.data_lake.types import PriceAdjustmentMode

MARKER_FILENAME = ".data-root.json"
MARKER_SCHEMA_VERSION = 1


class LakeRootIdentityError(RuntimeError):
    """Raised whenever a root's identity cannot be trusted.

    Covers a missing marker, a malformed one (bad JSON, wrong schema
    version, invalid UUID, unexpected fields), a marker whose UUID
    disagrees with what the caller expected, and every administrative
    refusal (stamping a populated root without the explicit force flag,
    re-initializing a root that already has one). Never raised as a side
    effect of a mutation that partially happened — every raise in this
    module happens before any file or catalog row is touched.
    """


class DataRootMarker(BaseModel):
    """The exact, closed shape of ``.data-root.json``. No timestamps, no
    host-specific paths — extra fields are rejected, not ignored, so the
    marker cannot silently grow one."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    data_root_id: UUID


def marker_path(base_root: Path) -> Path:
    """The one location a root's marker can live at."""
    return path_policy.lake_container_within(base_root) / MARKER_FILENAME


def read_marker(base_root: Path) -> DataRootMarker | None:
    """Parse the marker at ``base_root``, or ``None`` if it does not exist.

    Raises :class:`LakeRootIdentityError` for anything on disk that isn't a
    well-formed marker — malformed JSON, the wrong schema version, an
    invalid UUID, or extra fields. A marker that exists but cannot be
    trusted must never be treated the same as no marker at all.
    """
    path = marker_path(base_root)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LakeRootIdentityError(f"{path} is malformed: not valid JSON ({exc})") from exc
    try:
        marker = DataRootMarker.model_validate(raw)
    except ValidationError as exc:
        raise LakeRootIdentityError(f"{path} is malformed: {exc}") from exc
    if marker.schema_version != MARKER_SCHEMA_VERSION:
        raise LakeRootIdentityError(
            f"{path} has schema_version={marker.schema_version}, expected {MARKER_SCHEMA_VERSION}"
        )
    return marker


@dataclass(frozen=True)
class RootContext:
    """A validated physical lake root: its portable UUID plus the base path
    it currently happens to be mounted at.

    The base path is deliberately not part of the root's identity — only
    what :func:`resolve_root_context` used to confirm the marker at that
    path currently matches. Path resolution delegates to
    ``app.data_lake.path_policy``, the sole authority for lake path
    construction, so this object adds no second way to compute the same
    paths.
    """

    root_id: UUID
    base_root: Path

    def lake_container(self) -> Path:
        return path_policy.lake_container_within(self.base_root)

    def lake_root(self, price_adjustment_mode: PriceAdjustmentMode) -> Path:
        return path_policy.lake_root_within(self.base_root, price_adjustment_mode)

    def staging_root(self, price_adjustment_mode: PriceAdjustmentMode) -> Path:
        return path_policy.staging_root_within(self.base_root, price_adjustment_mode)


def resolve_root_context(expected_root_id: UUID, *, base_root: Path | None = None) -> RootContext:
    """Validate the root at ``base_root`` (default: the configured write
    root) actually is ``expected_root_id``, and return its context.

    This is what "normal application startup validates, never creates"
    (issue #1876) means concretely: it only ever reads the marker through
    :func:`read_marker`. A missing marker, a malformed one, or one whose
    UUID disagrees with ``expected_root_id`` all raise
    :class:`LakeRootIdentityError` — a populated root is never silently
    assigned the caller's expected identity.
    """
    resolved_base = base_root if base_root is not None else Path(settings.LEAN_DATA_WRITE_ROOT)
    marker = read_marker(resolved_base)
    if marker is None:
        raise LakeRootIdentityError(
            f"no root-identity marker at {marker_path(resolved_base)}; expected data_root_id={expected_root_id}. "
            f"Run scripts/manage_data_root.py init or stamp before starting the application against this root."
        )
    if marker.data_root_id != expected_root_id:
        raise LakeRootIdentityError(
            f"root-identity mismatch at {marker_path(resolved_base)}: marker says "
            f"data_root_id={marker.data_root_id}, expected {expected_root_id}"
        )
    return RootContext(root_id=expected_root_id, base_root=resolved_base)


def _is_empty_dir(path: Path) -> bool:
    return not path.exists() or next(path.iterdir(), None) is None


def _atomic_write_marker(base_root: Path, root_id: UUID) -> RootContext:
    """Write the marker at ``base_root`` exactly once, atomically.

    Stage-then-link within the same directory (never a cross-filesystem hop,
    since both live under ``base_root``): a reader can only ever see either
    no marker or a fully-written one, never a partial write from an
    interrupted process. ``os.link`` (not ``os.replace``) is what makes this
    create-if-absent rather than create-or-overwrite — it raises
    ``FileExistsError`` instead of silently clobbering a marker another
    concurrent caller just wrote, closing the TOCTOU window between the
    ``read_marker(...) is not None`` checks in :func:`init_empty_root` /
    :func:`stamp_existing_root` and this write.
    """
    container = path_policy.lake_container_within(base_root)
    container.mkdir(parents=True, exist_ok=True)
    final = marker_path(base_root)
    staged = container / f"{MARKER_FILENAME}.tmp-{os.getpid()}"
    payload = json.dumps({"schema_version": MARKER_SCHEMA_VERSION, "data_root_id": str(root_id)})
    with staged.open("w") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.link(staged, final)
    except FileExistsError as exc:
        raise LakeRootIdentityError(
            f"{final} was created concurrently by another process; refusing to overwrite it"
        ) from exc
    finally:
        os.unlink(staged)
    return RootContext(root_id=root_id, base_root=base_root)


def init_empty_root(base_root: Path, root_id: UUID) -> RootContext:
    """Administrative: mint a brand-new root's identity.

    Refuses if a marker already exists (re-initializing is
    :func:`stamp_existing_root`'s job, not this one's) or if the lake
    container holds anything at all — an empty root is the only thing this
    function is allowed to claim; a populated-but-unmarked root must go
    through :func:`stamp_existing_root`'s explicit force flag instead, so an
    operator cannot mint a competing identity for bytes another root
    believes are its own.
    """
    if read_marker(base_root) is not None:
        raise LakeRootIdentityError(f"{marker_path(base_root)} already exists; refusing to re-initialize it")
    container = path_policy.lake_container_within(base_root)
    if not _is_empty_dir(container):
        raise LakeRootIdentityError(
            f"{container} is populated; init_empty_root only claims an empty root. "
            f"Use stamp_existing_root(force=True) to stamp an existing canonical root instead."
        )
    return _atomic_write_marker(base_root, root_id)


def stamp_existing_root(base_root: Path, root_id: UUID, *, force: bool) -> RootContext:
    """Administrative: assign an identity to an existing (possibly
    populated) canonical root during rollout.

    Requires ``force=True`` — the explicit flag issue #1876 requires for
    this path — precisely because it is the one operation allowed to claim
    a populated root; the flag is the operator's affirmative statement that
    they know what they are stamping. Still refuses outright if a marker
    already exists, force or not: this stamps a root's *first* identity, it
    does not relabel one.

    A **populated** root may only be stamped with :data:`LEGACY_ROOT_ID`.
    The EF migration that shipped alongside this issue backfilled every
    pre-existing ``DataLakeArtifacts`` row to that same
    nil UUID; stamping the physical root that holds that data with any
    other UUID would silently orphan it from every root-scoped catalog
    read. An empty root has no existing catalog rows to orphan, so it may
    still take any UUID — that is the genuine "new secondary root" path
    :func:`init_empty_root` normally covers.
    """
    if not force:
        raise LakeRootIdentityError(
            "stamping an existing root requires the explicit force flag "
            "(this claims whatever is already on disk as this root's identity)"
        )
    if read_marker(base_root) is not None:
        raise LakeRootIdentityError(f"{marker_path(base_root)} already exists; refusing to overwrite it")
    container = path_policy.lake_container_within(base_root)
    if not _is_empty_dir(container) and root_id != LEGACY_ROOT_ID:
        raise LakeRootIdentityError(
            f"{container} is populated; stamping a populated root requires root_id={LEGACY_ROOT_ID} "
            f"(the id the migration backfilled existing catalog rows to), got {root_id}. "
            f"Use init_empty_root for a genuinely new, empty secondary root."
        )
    return _atomic_write_marker(base_root, root_id)


def inspect_root(base_root: Path) -> DataRootMarker | None:
    """Administrative: read a root's marker without changing anything.

    Thin wrapper over :func:`read_marker` — kept as its own name because it
    is one of the three administrative operations issue #1876 requires a
    CLI command for (init / stamp / inspect), and callers reading for
    inspection shouldn't have to reach for the lower-level function that
    :func:`resolve_root_context` also builds on.
    """
    return read_marker(base_root)
