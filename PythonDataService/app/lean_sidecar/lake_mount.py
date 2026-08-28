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
:data:`CONTAINER_LAKE_DATA_MOUNT` — which is the reader-side container
path the same spec section names (``LEAN_DATA_ROOT=/lean-data``) — and
LEAN's ``data-folder`` is re-pointed there by config rendering.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from app.data_lake.path_policy import LeanDailyBarPath, LeanMetadataPath, LeanMinuteBarPath
from app.lean_sidecar.workspace import validate_symbol

if TYPE_CHECKING:
    from app.engine.data.trade_bar import TradeBar

logger = logging.getLogger(__name__)

# ``app.config`` and the engine reader are imported inside the functions
# that need them, not at module scope. The launcher is a standalone host
# process that imports this module only for :class:`LakeMount`; it has
# neither the data plane's ``Settings`` environment (importing
# ``app.config`` without ``POLYGON_API_KEY`` raises) nor any business
# reading bars.

# Container-side target of the lake's read-only mount. Named to match
# the spec's reader-side env var (``LEAN_DATA_ROOT=/lean-data``); see
# the module docstring for why it is not ``/lean-run/data``.
CONTAINER_LAKE_DATA_MOUNT = "/lean-data"

# Subdirectory of the deploy volume holding the immutable lake. The
# writer-side counterpart is ``app.data_lake.ensure_data._lake_roots``,
# which derives ``<LEAN_DATA_WRITE_ROOT>/lake`` from the same setting;
# ``tests/lean_sidecar/test_lake_mount.py`` pins the two in lockstep.
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

    @property
    def container_target(self) -> str:
        return CONTAINER_LAKE_DATA_MOUNT

    @property
    def mode(self) -> Literal["ro"]:
        return "ro"

    def volume_argument(self) -> str:
        """Render the ``-v`` value podman receives."""
        return f"{self.host_lake_root}:{self.container_target}:{self.mode}"


def lake_mount_enabled() -> bool:
    """True when sidecar runs should read the lake instead of staging."""
    from app.config import settings

    return bool(settings.DATA_LAKE_ENABLED)


def data_plane_lake_root() -> Path:
    """The lake root as *this* process sees it.

    The data plane holds a read view of the same volume the writer owns,
    so the root is derived from the writer setting rather than from a
    second knob that could drift out of agreement with it.
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
class LakeBarStream:
    """The lake artifacts one run exposes to LEAN, plus their bars.

    ``bars_by_date`` is decoded by the canonical LEAN reader
    (:class:`app.engine.data.lean_format.LeanMinuteDataReader`) — the
    same reader the Python engine uses — straight off the lake files
    named in ``trade_zip_paths``. No copy and no re-encode sits between
    the two, which is what makes the bytes LEAN mounts and the bars the
    Python readers produce provably the same artifacts.
    """

    lake_root: Path
    trading_dates: tuple[date, ...]
    bars_by_date: tuple[tuple[date, list[TradeBar]], ...]
    trade_zip_paths: tuple[Path, ...]
    quote_zip_paths: tuple[Path, ...]
    daily_zip_path: Path


def read_lake_bar_stream(
    *,
    lake_root: Path,
    symbol: str,
    start: date,
    end: date,
    session: Literal["regular", "extended"],
) -> LakeBarStream:
    """Collect the lake artifacts for ``symbol`` over ``[start, end]``.

    Purely a read: the lake is never written, never fetched into, and
    never copied out of. Days the lake does not cover are simply absent
    from the result — the caller decides whether partial coverage is
    acceptable — but a window with *no* trade artifact at all, or a
    symbol with no daily artifact, raises :class:`LakeMountError`
    because LEAN cannot run on either.
    """
    from app.engine.data.lean_format import LeanMinuteDataReader

    safe_symbol = validate_symbol(symbol)
    reader = LeanMinuteDataReader([lake_root], session=session)

    trading_dates: list[date] = []
    bars_by_date: list[tuple[date, list[TradeBar]]] = []
    trade_zip_paths: list[Path] = []
    quote_zip_paths: list[Path] = []
    for trading_date in reader.iter_dates(safe_symbol, start, end):
        bars = reader.read_day(safe_symbol, trading_date)
        if not bars:
            continue
        trading_dates.append(trading_date)
        bars_by_date.append((trading_date, bars))
        trade_zip_paths.append(_lake_minute_path(lake_root, safe_symbol, trading_date, "trade"))
        quote_path = _lake_minute_path(lake_root, safe_symbol, trading_date, "quote")
        if quote_path.exists():
            quote_zip_paths.append(quote_path)

    if not bars_by_date:
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

    logger.info(
        "lean sidecar reading lake artifacts",
        extra={
            "symbol": safe_symbol,
            "lake_root": str(lake_root),
            "trading_days": len(trading_dates),
            "quote_artifacts": len(quote_zip_paths),
        },
    )
    return LakeBarStream(
        lake_root=lake_root,
        trading_dates=tuple(trading_dates),
        bars_by_date=tuple(bars_by_date),
        trade_zip_paths=tuple(trade_zip_paths),
        quote_zip_paths=tuple(quote_zip_paths),
        daily_zip_path=daily_zip_path,
    )


def lake_metadata_paths(lake_root: Path) -> tuple[Path | None, Path | None]:
    """Return (market_hours_db, symbol_properties_db) present in the lake.

    Lake-mode counterpart of
    :func:`app.lean_sidecar.staging.list_metadata_databases`: in lake
    mode LEAN's data folder IS the lake, so the metadata databases the
    manifest must hash are the lake's Phase-0 bootstrap artifacts rather
    than the per-run image extraction. ``None`` for an absent file, so
    the manifest records absence rather than inventing a hash.
    """
    return (
        _lake_metadata_path(lake_root, "market_hours"),
        _lake_metadata_path(lake_root, "symbol_properties"),
    )


def _lake_metadata_path(
    lake_root: Path,
    kind: Literal["market_hours", "symbol_properties"],
) -> Path | None:
    relative = LeanMetadataPath(kind=kind).relative_path()
    candidate = lake_root / Path(*relative.parts)
    return candidate if candidate.exists() else None


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
