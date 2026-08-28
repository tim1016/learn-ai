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

Known properties of this design, recorded not hidden
----------------------------------------------------
**Manifest fidelity covers declared inputs; the mount does not prevent
undeclared reads.** The whole lake is mounted, so an arbitrary
``algorithm_source`` can subscribe to symbols and dates the run never
declared, while the manifest hashes only the declared inputs
(:func:`resolve_lake_artifacts`'s result). Staging mode did not have
this property: each run got a private data folder containing exactly
its own bytes, so "what the manifest hashes" and "what LEAN could read"
were the same set.

This is inherent to the spec's zero-copy mount-table design, not an
oversight in this module, and per-run scoped mounts are explicitly *not*
the fix — they would reintroduce the per-run copy the lake exists to
eliminate. It is a deliberate trade: reproducibility evidence for
declared inputs, in exchange for the copy. Recorded for the integration
slice, which owns whether an undeclared-read *detection* (rather than
prevention) is warranted.

**Lake metadata bytes are not verified against the pinned LEAN image
digest.** ``ensure_data`` records the ``lean_image_digest`` its
market-hours / symbol-properties bytes came from in the *catalog*
(``data_contract_hash``), not beside the files, so a lake bootstrapped
under image A can serve a run pinned to image B and this module cannot
tell. Detecting it needs a catalog read this module deliberately does
not do — the sidecar's lake access is filesystem-only. Recorded for the
integration slice, which already talks to the catalog.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from app.data_lake.path_policy import (
    LeanDailyBarPath,
    LeanFactorFilePath,
    LeanMapFilePath,
    LeanMetadataPath,
    LeanMinuteBarPath,
)
from app.lean_sidecar.trading_calendar import expected_sessions
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
    candidate = Path(raw)
    if not candidate.is_absolute():
        # A relative value is not merely awkward, it is ambiguous
        # between two readers that both look correct: podman resolves
        # it against the launcher process's cwd, while compose resolves
        # it against the directory holding compose.yaml. The two
        # disagree in exactly the deployments where the lake matters,
        # and the symptom (an empty or wrong data folder) does not point
        # back at the setting. Refuse rather than pick one.
        raise LakeMountError(
            f"lake_volume_host_path_not_absolute: {LAKE_VOLUME_HOST_PATH_ENV}={raw!r} is relative. "
            f"Podman would resolve it against the launcher's working directory "
            f"({Path.cwd()}), compose against the directory holding compose.yaml; "
            "set an absolute path so both agree."
        )
    return candidate.resolve() / LAKE_SUBDIR


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
    factor_file_paths: tuple[Path, ...]
    map_file_paths: tuple[Path, ...]


def resolve_lake_artifacts(
    *,
    lake_root: Path,
    symbol: str,
    start: date,
    end: date,
) -> LakeArtifacts:
    """Resolve the lake artifacts for ``symbol`` over ``[start, end]``.

    Purely a read, and not even a decode of the minute artifacts: the
    lake is never written, never fetched into, and never copied out of.

    **The materialization seam refuses what it cannot fully serve.**
    Every NYSE session in the requested window must have both a trade
    and a quote artifact, the daily artifact must actually span the
    window, and both required metadata databases must be present.
    Anything short of that raises :class:`LakeMountError` naming the
    shortfall, before a container is launched.

    Silently narrowing to the sessions that happen to be present is the
    failure mode this exists to prevent: the rendered ``config.json``
    still declares the full requested window, so LEAN would run a
    shorter backtest than the manifest, the UI, and the operator all
    believe it ran. A gap in the lake is a coverage bug to fix upstream,
    not a window to quietly trim.

    Note the deliberate asymmetry with the staging path, which may run
    with no metadata databases at all and let LEAN fall back to the
    image defaults, and which synthesizes quote zips on the fly. Neither
    fallback exists here: lake mode re-points ``data-folder`` away from
    the image-extracted workspace copy and the mount is read-only, so
    what the lake lacks cannot be conjured for this run.
    """
    safe_symbol = validate_symbol(symbol)
    required_sessions = expected_sessions(start, end)
    if not required_sessions:
        raise LakeMountError(
            f"lake_window_has_no_sessions: {start.isoformat()}..{end.isoformat()} contains no NYSE trading sessions"
        )

    trade_zip_paths: list[Path] = []
    quote_zip_paths: list[Path] = []
    missing_trade: list[date] = []
    missing_quote: list[date] = []
    for session in required_sessions:
        trade_path = _lake_minute_path(lake_root, safe_symbol, session, "trade")
        if trade_path.exists():
            trade_zip_paths.append(trade_path)
        else:
            missing_trade.append(session)
        quote_path = _lake_minute_path(lake_root, safe_symbol, session, "quote")
        if quote_path.exists():
            quote_zip_paths.append(quote_path)
        else:
            missing_quote.append(session)

    if missing_trade:
        raise LakeMountError(
            f"lake_incomplete_trade_coverage: {safe_symbol} is missing minute-trade artifacts "
            f"for {_render_sessions(missing_trade)} of the {len(required_sessions)} NYSE "
            f"sessions in {start.isoformat()}..{end.isoformat()} under {lake_root}"
        )
    if missing_quote:
        # LEAN's default minute subscription requests trade AND quote;
        # staging synthesizes the quote zips for exactly this reason.
        # Launching without them guarantees failed_data_requests.
        raise LakeMountError(
            f"lake_incomplete_quote_coverage: {safe_symbol} is missing minute-quote artifacts "
            f"for {_render_sessions(missing_quote)}; LEAN's default minute subscription "
            "requests quotes and the read-only mount cannot synthesize them"
        )

    daily_zip_path = _require_daily_artifact_covering(lake_root, safe_symbol, required_sessions)
    market_hours_path, symbol_properties_path = require_lake_metadata(lake_root)

    logger.info(
        "lean sidecar resolving lake artifacts",
        extra={
            "symbol": safe_symbol,
            "lake_root": str(lake_root),
            "trading_days": len(required_sessions),
        },
    )
    return LakeArtifacts(
        lake_root=lake_root,
        trading_dates=tuple(required_sessions),
        trade_zip_paths=tuple(trade_zip_paths),
        quote_zip_paths=tuple(quote_zip_paths),
        daily_zip_path=daily_zip_path,
        market_hours_path=market_hours_path,
        symbol_properties_path=symbol_properties_path,
        factor_file_paths=_existing_corporate_action_files(lake_root, safe_symbol, "factor"),
        map_file_paths=_existing_corporate_action_files(lake_root, safe_symbol, "map"),
    )


_MAX_RENDERED_SESSIONS = 5


def _render_sessions(sessions: list[date]) -> str:
    """Name the missing sessions without pasting a two-year window into a log."""
    shown = ", ".join(d.isoformat() for d in sessions[:_MAX_RENDERED_SESSIONS])
    if len(sessions) > _MAX_RENDERED_SESSIONS:
        return f"{len(sessions)} sessions ({shown}, ...)"
    return f"{len(sessions)} sessions ({shown})"


def _require_daily_artifact_covering(
    lake_root: Path,
    symbol: str,
    required_sessions: list[date],
) -> Path:
    """Return the daily artifact, or raise if it does not span the window.

    Existence alone is not coverage. The daily zip holds a symbol's
    whole history in one file, so a zip written for an earlier, shorter
    window survives a later window extension untouched and passes any
    ``exists()`` check while silently lacking the new dates. LEAN would
    resolve its benchmark against a truncated equity history.

    This is the one artifact worth decoding: it is a single file per
    symbol, not one per session, so reading it costs a single unzip
    rather than O(window).
    """
    from app.engine.data.lean_format import LeanDailyDataReader

    daily_zip_path = _lake_daily_path(lake_root, symbol)
    if not daily_zip_path.exists():
        raise LakeMountError(
            f"lake_missing_daily_artifact: {daily_zip_path} is absent; LEAN needs the "
            "daily artifact for benchmark resolution"
        )

    covered = set(LeanDailyDataReader([lake_root]).available_dates(symbol))
    missing = [session for session in required_sessions if session not in covered]
    if missing:
        raise LakeMountError(
            f"lake_daily_artifact_does_not_cover_window: {daily_zip_path} is missing "
            f"{_render_sessions(missing)} of the requested window; it predates a window "
            "extension and must be rebuilt before this run can be served"
        )
    return daily_zip_path


def _existing_corporate_action_files(
    lake_root: Path,
    symbol: str,
    kind: Literal["factor", "map"],
) -> tuple[Path, ...]:
    """Return the symbol's factor or map file if the lake carries one.

    Unlike bars and metadata these are genuinely optional — a symbol
    with no corporate actions in the window has nothing to declare — so
    absence is not an error. Presence, however, is load-bearing: LEAN
    reads these off the mount and they change split/dividend-adjusted
    results, so whatever is there must reach the manifest hash. See
    ``lean_sidecar_service._build_manifest``.
    """
    relative = (
        LeanFactorFilePath(market="usa", symbol=symbol).relative_path()
        if kind == "factor"
        else LeanMapFilePath(market="usa", symbol=symbol).relative_path()
    )
    candidate = lake_root / Path(*relative.parts)
    return (candidate,) if candidate.exists() else ()


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
