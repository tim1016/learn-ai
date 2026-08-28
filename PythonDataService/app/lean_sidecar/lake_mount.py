"""Read-only data-lake mount for LEAN sidecar runs (flag-gated).

Today a sidecar run *stages* Polygon-canonical bars: it materializes
missing days into the policy-keyed bar store and byte-copies the day
zips into ``workspace/data`` (see
:func:`app.lean_sidecar.staging.stage_minute_zips_from_store`). Every
run therefore owns a private copy of the same bytes.

When ``DATA_LAKE_ENABLED`` is on, the run reads the lake instead: the
immutable lake subtree is bind-mounted **read-only** into the LEAN
container and LEAN's ``data-folder`` points at that mount. Nothing is
copied, nothing is fetched per run, and a crashing container cannot
mutate the lake because the mount carries ``:ro`` at the OS level.

Authority: the 2026-05-20 data-lake design spec, § 2.2 "Volume layout
(host-bind)" + its container mount table, and § 5.3 (LEAN path policy).
The spec was pruned from the tree; recover it with::

    git show 8441f4f6^:docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md

Deliberate deviation from the spec's mount table
------------------------------------------------
The spec mounts ``${VOLUME}/lake`` at ``/lean-run/data`` because in its
design the *workspace* is mounted as six separate subdirectory mounts
(``/lean-run/algorithm``, ``/lean-run/config``, ...). This
implementation mounts the whole workspace at ``/lean-run`` (see
:data:`app.lean_sidecar.runner.CONTAINER_WORKSPACE_MOUNT`), so a lake
mount at ``/lean-run/data`` would nest inside the read-write workspace
mount, shadow ``workspace/data``, and depend on podman's mount-ordering
semantics. The lake is therefore mounted at a sibling target,
:data:`CONTAINER_LAKE_DATA_MOUNT`, and LEAN's ``data-folder`` is
re-pointed there by config rendering.

The literal string ``/lean-data`` is borrowed from that same spec
section, but note what it means there: ``LEAN_DATA_ROOT=/lean-data`` is
the **Python data service's** read mount, not the LEAN container's. The
spec assigns the LEAN container no target other than ``/lean-run/data``,
so this is a new target chosen for symmetry with the reader's — not one
the spec sanctions for this container.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from app.data_lake.path_policy import LeanDailyBarPath, LeanMetadataPath, LeanMinuteBarPath
from app.lean_sidecar.workspace import validate_symbol

logger = logging.getLogger(__name__)

# ``app.config`` is imported inside the two functions that need it, not
# at module scope. The launcher is a standalone host process that
# imports this module only for :class:`LakeMount`, and does not have the
# data plane's ``Settings`` environment (importing ``app.config``
# without ``POLYGON_API_KEY`` raises).

# Container-side target of the lake's read-only mount. See the module
# docstring for why it is not ``/lean-run/data`` and for what
# ``/lean-data`` does and does not mean in the spec.
CONTAINER_LAKE_DATA_MOUNT = "/lean-data"

# Subdirectory of the deploy volume holding the immutable lake. The
# writer-side counterpart is ``app.data_lake.ensure_data._lake_roots``,
# which derives ``<LEAN_DATA_WRITE_ROOT>/lake`` from the same setting;
# ``tests/lean_sidecar/test_lake_mount.py`` pins the two in lockstep.
#
# The duplicate is deliberate and temporary: the adoption slice that
# would host a shared resolver is in flight against ``ensure_data.py``
# at the same time as this one, and editing that file concurrently
# costs more than one constant. The integration slice collapses both
# onto a single resolver and deletes the parity test with this comment.
LAKE_SUBDIR = "lake"

# Deploy-time env naming the **host** path of the lake volume. The
# launcher (a host process with podman) resolves the mount source from
# it; the data plane never sends a path, so a caller cannot widen or
# redirect the mount.
LAKE_VOLUME_HOST_PATH_ENV = "LEAN_DATA_VOLUME_HOST_PATH"


class LakeMountError(RuntimeError):
    """The lake cannot satisfy this run.

    Raised instead of silently falling back to per-run staging: a
    lake-mode run that quietly re-fetched from Polygon would defeat the
    point of the flag and hide a coverage gap.
    """


@dataclass(frozen=True, slots=True)
class LakeMount:
    """One read-only bind mount of the lake into the LEAN container.

    The container target and the ``ro`` mode are **structural**, not
    fields: there is no constructor argument that produces a writable
    lake mount, so "the mount is read-only" is a property of the type
    rather than of every call site. ``build_command`` renders
    :meth:`volume_argument` verbatim into the podman argv.
    """

    host_lake_root: Path

    def volume_argument(self) -> str:
        """Render the ``-v`` value podman receives."""
        return f"{self.host_lake_root}:{CONTAINER_LAKE_DATA_MOUNT}:ro"


def lake_mount_enabled() -> bool:
    """True when sidecar runs should read the lake instead of staging."""
    from app.config import settings

    return bool(settings.DATA_LAKE_ENABLED)


def data_plane_lake_root() -> Path:
    """The lake root as *this* process sees it.

    Derived from ``LEAN_DATA_WRITE_ROOT`` because that is the mount the
    data plane actually has: compose gives this container one
    read-write mount of the volume, and the sidecar reads the lake
    through it. The spec's separate read-only reader mount
    (``LEAN_DATA_ROOT``) does not exist in this deployment yet; when it
    does, this is the one function that changes.
    """
    from app.config import settings

    return Path(settings.LEAN_DATA_WRITE_ROOT) / LAKE_SUBDIR


def launcher_host_lake_root() -> Path | None:
    """The lake root as the *launcher host* sees it, or None if unset.

    Resolved from :data:`LAKE_VOLUME_HOST_PATH_ENV` exactly the way the
    launcher resolves its artifacts root — deploy-time configuration,
    never request payload.

    Deliberately does NOT create the directory. Unlike the artifacts
    root, which the launcher owns and fills, the lake is written by the
    data plane's lake writer; conjuring an empty one here would turn
    "the lake volume is not mounted on this host" into "every trading
    day is missing data". The runner's ``is_dir`` check rejects the
    launch instead.
    """
    raw = os.environ.get(LAKE_VOLUME_HOST_PATH_ENV)
    if not raw:
        return None
    return Path(raw).resolve() / LAKE_SUBDIR


@dataclass(frozen=True, slots=True)
class LakeArtifacts:
    """Exactly which lake files this run exposes to LEAN.

    Paths only, deliberately: in lake mode nothing is copied or
    re-encoded, so the run has no use for decoded bars — LEAN decodes
    them itself from the mount. Resolving the set is pure ``exists()``
    checks, which keeps a long window off the "unzip every day on the
    event loop" path the staging mode needs.
    """

    lake_root: Path
    trading_dates: tuple[date, ...]
    trade_zip_paths: tuple[Path, ...]
    quote_zip_paths: tuple[Path, ...]
    daily_zip_path: Path
    market_hours_path: Path
    symbol_properties_path: Path


def resolve_lake_artifacts(
    *,
    lake_root: Path,
    symbol: str,
    start: date,
    end: date,
) -> LakeArtifacts:
    """Resolve the lake artifacts for ``symbol`` over ``[start, end]``.

    Purely a read, and not even a decode: the lake is never written,
    never fetched into, and never copied out of.

    Partial coverage is allowed — days the lake does not cover are
    simply absent from the result, and the caller decides whether that
    is acceptable, matching what the staging path does. Everything LEAN
    *cannot start without* fails loud instead: no trade artifact at all
    in the window, a missing daily artifact, or a missing required
    metadata database each raise :class:`LakeMountError` here, before a
    container is launched.

    Note the deliberate asymmetry with the staging path, which may run
    with no metadata databases at all and let LEAN fall back to the
    image defaults. That fallback does not exist here: lake mode
    re-points ``data-folder`` away from the image-extracted workspace
    copy, so absent metadata in the lake is a hard stop, not a
    degraded-but-working run.
    """
    safe_symbol = validate_symbol(symbol)

    trading_dates: list[date] = []
    trade_zip_paths: list[Path] = []
    quote_zip_paths: list[Path] = []
    trading_date = start
    one_day = timedelta(days=1)
    while trading_date <= end:
        trade_path = _lake_minute_path(lake_root, safe_symbol, trading_date, "trade")
        if trade_path.exists():
            trading_dates.append(trading_date)
            trade_zip_paths.append(trade_path)
            quote_path = _lake_minute_path(lake_root, safe_symbol, trading_date, "quote")
            if quote_path.exists():
                quote_zip_paths.append(quote_path)
        trading_date += one_day

    if not trade_zip_paths:
        raise LakeMountError(
            f"lake_window_empty: no minute-trade artifacts for {safe_symbol} in "
            f"{start.isoformat()}..{end.isoformat()} under {lake_root}"
        )

    daily_zip_path = _lake_daily_path(lake_root, safe_symbol)
    if not daily_zip_path.exists():
        raise LakeMountError(
            f"lake_missing_daily_artifact: {daily_zip_path} is absent; LEAN needs the "
            "daily artifact for benchmark resolution"
        )

    market_hours_path, symbol_properties_path = require_lake_metadata(lake_root)

    logger.info(
        "lean sidecar resolving lake artifacts",
        extra={
            "symbol": safe_symbol,
            "lake_root": str(lake_root),
            "trading_days": len(trading_dates),
            "quote_artifacts": len(quote_zip_paths),
        },
    )
    return LakeArtifacts(
        lake_root=lake_root,
        trading_dates=tuple(trading_dates),
        trade_zip_paths=tuple(trade_zip_paths),
        quote_zip_paths=tuple(quote_zip_paths),
        daily_zip_path=daily_zip_path,
        market_hours_path=market_hours_path,
        symbol_properties_path=symbol_properties_path,
    )


# Every metadata kind the lake bootstraps is one LEAN refuses to
# initialize without (see
# ``staging.stage_lean_metadata_from_image``'s docstring), so the lake
# has no optional metadata today. A future optional kind belongs in a
# separate accessor that may return ``None``, not in this one.
REQUIRED_LAKE_METADATA: tuple[Literal["market_hours", "symbol_properties"], ...] = (
    "market_hours",
    "symbol_properties",
)


def require_lake_metadata(lake_root: Path) -> tuple[Path, Path]:
    """Return (market_hours_db, symbol_properties_db) or raise.

    Lake-mode counterpart of
    :func:`app.lean_sidecar.staging.list_metadata_databases`, but a
    demand rather than a survey: in lake mode LEAN's data folder IS the
    lake, and LEAN refuses to initialize when either database is
    missing from it. Returning ``None`` here would defer that certain
    failure to a launched container, and would let the manifest record
    "no metadata" as though it were a legitimate shape.
    """
    paths = {kind: _lake_metadata_path(lake_root, kind) for kind in REQUIRED_LAKE_METADATA}
    missing = sorted(kind for kind, path in paths.items() if not path.exists())
    if missing:
        raise LakeMountError(
            f"lake_missing_required_metadata: {missing} absent under {lake_root}; "
            "LEAN refuses to initialize without them and lake mode has no image-extracted fallback"
        )
    return paths["market_hours"], paths["symbol_properties"]


def _lake_metadata_path(
    lake_root: Path,
    kind: Literal["market_hours", "symbol_properties"],
) -> Path:
    """Where the metadata database belongs, present or not."""
    relative = LeanMetadataPath(kind=kind).relative_path()
    return lake_root / Path(*relative.parts)


def _lake_minute_path(
    lake_root: Path,
    symbol: str,
    trading_date: date,
    data_type: Literal["trade", "quote"],
) -> Path:
    """Absolute path of one minute artifact, via the lake path policy."""
    relative = LeanMinuteBarPath(
        market="usa",
        symbol=symbol,
        trading_date=trading_date,
        data_type=data_type,
    ).relative_path()
    return lake_root / Path(*relative.parts)


def _lake_daily_path(lake_root: Path, symbol: str) -> Path:
    """Absolute path of the daily artifact, via the lake path policy."""
    relative = LeanDailyBarPath(market="usa", symbol=symbol).relative_path()
    return lake_root / Path(*relative.parts)
