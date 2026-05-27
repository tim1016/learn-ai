"""Workspace data, config, and source staging.

The data plane (``polygon-data-service``) populates a workspace before
calling the launcher. ``staging.py`` is the single seam where Polygon
data turns into LEAN's on-disk format and the algorithm source + config
are placed next to it. The launcher does no staging — staging happens
out-of-process and the launcher only invokes the container.

Authority: ``docs/architecture/lean-sidecar-lab.md`` §"Workspace
contract" and §"LEAN data-folder fidelity".
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.engine.data.lean_format import (
    write_lean_daily_zip,
    write_lean_day_zip,
    write_lean_quote_day_zip,
)
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.config import LEAN_IMAGE_REPO
from app.lean_sidecar.lean_config import LeanConfig
from app.lean_sidecar.workspace import Workspace, validate_symbol

logger = logging.getLogger(__name__)

# Paths inside the LEAN image where the bundled metadata + alternative
# databases live. These ship with the image; extracting them per-run
# keeps the manifest hashing surface uniform (every file LEAN reads
# lives under the workspace and is hashed).
IMAGE_LEAN_DATA_ROOT = "/Lean/Data"
IMAGE_MARKET_HOURS = f"{IMAGE_LEAN_DATA_ROOT}/market-hours"
IMAGE_SYMBOL_PROPERTIES = f"{IMAGE_LEAN_DATA_ROOT}/symbol-properties"
IMAGE_INTEREST_RATE = f"{IMAGE_LEAN_DATA_ROOT}/alternative/interest-rate"


class MetadataStagingError(RuntimeError):
    """The image-bundled metadata could not be extracted."""


@dataclass(frozen=True, slots=True)
class StagedRun:
    """Materialized view of what is on disk before the launcher runs.

    Returned by :func:`stage_python_run` so the manifest writer can
    hash exactly the files that ended up in the workspace, rather than
    re-scanning the directory tree and risk capturing artifacts a
    previous run left behind.
    """

    workspace: Workspace
    algorithm_source_path: Path
    config_path: Path
    bar_zip_paths: tuple[Path, ...]
    market_hours_path: Path | None
    symbol_properties_path: Path | None
    factor_files: tuple[Path, ...]
    map_files: tuple[Path, ...]


def stage_algorithm_source(workspace: Workspace, source: str) -> Path:
    """Write the Python algorithm source to ``workspace/project/main.py``.

    Single seam for the Phase-1 trusted-sample path and (later) the
    arbitrary-source path. The launcher boundary never re-reads or
    rewrites this file; what is on disk at staging time is what LEAN
    sees.
    """
    workspace.ensure_layout()
    dest = workspace.project_dir / "main.py"
    dest.write_text(source, encoding="utf-8")
    return dest


def stage_lean_config(workspace: Workspace, config: LeanConfig) -> Path:
    """Write the LEAN ``config.json`` to ``workspace/project/config.json``."""
    workspace.ensure_layout()
    return config.write(workspace.project_dir / "config.json")


def stage_minute_bars(
    workspace: Workspace,
    *,
    symbol: str,
    bars_by_date: Iterable[tuple[date, list[TradeBar]]],
) -> tuple[Path, ...]:
    """Stage per-day LEAN minute zips under ``workspace/data/equity/usa/minute/``.

    Delegates to :func:`app.engine.data.lean_format.write_lean_day_zip`
    so the same writer that backs Engine Lab's local cache is the one
    writing LEAN's sidecar data folder. There is exactly one writer for
    the deci-cent / ms-since-midnight contract.

    ``symbol`` is re-validated via :func:`validate_symbol` even when
    the caller already validated upstream — staging is the place
    where the symbol first flows into a filesystem path, so the
    defense-in-depth check belongs here.
    """
    safe_symbol = validate_symbol(symbol)
    workspace.ensure_layout()
    written: list[Path] = []
    for trading_date, bars in bars_by_date:
        path = write_lean_day_zip(
            workspace.data_dir,
            safe_symbol,
            trading_date,
            bars,
        )
        written.append(path)
    return tuple(written)


def stage_quote_bars(
    workspace: Workspace,
    *,
    symbol: str,
    bars_by_date: Iterable[tuple[date, list[TradeBar]]],
) -> tuple[Path, ...]:
    """Stage per-day LEAN minute QUOTE zips alongside the trade zips.

    Phase 5c — eliminates the known-noise ``Cannot find file:
    ...quote.zip`` log lines the launcher's result_classifier was
    treating as expected ``failed_data_requests``. Each quote zip is
    synthesized from the same TradeBar list as the matching trade
    zip (bid = ask = trade close, size = 0); see
    :func:`write_lean_quote_day_zip` for the spread/size rationale.

    Re-validates ``symbol`` for the same defense-in-depth reason as
    :func:`stage_minute_bars`.
    """
    safe_symbol = validate_symbol(symbol)
    workspace.ensure_layout()
    written: list[Path] = []
    for trading_date, bars in bars_by_date:
        path = write_lean_quote_day_zip(
            workspace.data_dir,
            safe_symbol,
            trading_date,
            bars,
        )
        written.append(path)
    return tuple(written)


def stage_daily_bars(
    workspace: Workspace,
    *,
    symbol: str,
    bars: list[TradeBar],
) -> Path:
    """Stage a LEAN daily zip under ``workspace/data/equity/usa/daily/``.

    Phase 1 trusted-sample wiring needs at least one daily bar zip so
    LEAN's default benchmark resolution and the post-run
    ResultsAnalyzer's equity-curve construction do not fail. The shape
    matches :func:`app.engine.data.lean_format.write_lean_daily_zip`.

    Re-validates ``symbol`` for the same defense-in-depth reason as
    :func:`stage_minute_bars`.
    """
    safe_symbol = validate_symbol(symbol)
    workspace.ensure_layout()
    return write_lean_daily_zip(workspace.data_dir, safe_symbol, bars)


def stage_empty_corporate_action_dirs(workspace: Workspace) -> None:
    """Create empty ``factor_files`` / ``map_files`` subdirectories.

    A reconciliation-grade run requires real factor and map files (see
    ADR §"Corporate actions and metadata policy"). For the
    non-reconciliation trusted sample the windows have no corporate
    actions, so an empty directory is enough to silence LEAN's
    ``LocalDiskMapFileProvider`` warning and keep the run output
    classified as clean.
    """
    workspace.ensure_layout()
    for sub in ("factor_files", "map_files"):
        (workspace.data_dir / "equity" / "usa" / sub).mkdir(parents=True, exist_ok=True)


def list_factor_map_files(workspace: Workspace) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """List staged factor and map files under the workspace data dir.

    Empty tuples are returned when no files are present — that is the
    Phase 1 trusted-sample case (no corporate actions in window) and is
    intentionally distinct from the reconciliation-grade requirement
    that both lists be non-empty for affected symbols.
    """
    factor_root = workspace.data_dir / "equity" / "usa" / "factor_files"
    map_root = workspace.data_dir / "equity" / "usa" / "map_files"
    factors = tuple(sorted(factor_root.rglob("*.csv"))) if factor_root.exists() else ()
    maps = tuple(sorted(map_root.rglob("*.csv"))) if map_root.exists() else ()
    return factors, maps


def list_metadata_databases(
    workspace: Workspace,
) -> tuple[Path | None, Path | None]:
    """Return (market_hours_db, symbol_properties_db) paths if present.

    Reconciliation-grade runs require both; trusted-sample non-
    reconciliation runs may omit them and inherit the LEAN image
    defaults. The manifest distinguishes the two cases by recording
    whether the file existed at staging time, not by inferring from the
    run.
    """
    mh = workspace.data_dir / "market-hours" / "market-hours-database.json"
    sp = workspace.data_dir / "symbol-properties" / "symbol-properties-database.csv"
    return (mh if mh.exists() else None, sp if sp.exists() else None)


def stage_lean_metadata_from_image(
    workspace: Workspace,
    image_digest: str,
    *,
    allow_launcher_fallback: bool = True,
) -> tuple[Path, Path]:
    """Extract the image's bundled metadata databases into the workspace.

    LEAN refuses to initialize when the symbol-properties or market-hours
    database is missing from the configured ``data-folder``. Those files
    ship inside the image at ``/Lean/Data/{market-hours,symbol-properties}/``;
    this helper copies them into the workspace's ``data/`` subtree so
    they sit under the single mount LEAN sees and so the manifest can
    hash exactly what LEAN read.

    Uses ``podman cp`` against a freshly-created (but not started)
    container, then removes the container — no LEAN process runs and no
    network is required.

    When ``shutil.which("podman")`` returns None — the data-plane
    container's expected state — falls back to calling the launcher's
    ``/extract-metadata`` HTTP endpoint via
    :func:`_stage_lean_metadata_via_launcher`. The launcher (host
    process with podman) does the work; the data-plane reads the
    written files through its bind-mounted view.

    Args:
        workspace: Resolved workspace; ``data_dir`` is created if missing.
        image_digest: ``sha256:...`` (or full ``repo@sha256:...``) to extract
            from. Must already be pulled locally; this helper does not pull.
        allow_launcher_fallback: Default ``True`` — the data plane wants
            the HTTP fallback when its container has no podman. The
            launcher passes ``False`` when it calls into this function
            itself, so a launcher host that's missing podman fails fast
            instead of HTTP-POSTing /extract-metadata back into its own
            handler (infinite recursion until httpx times out).

    Returns:
        Tuple of (market_hours_db_path, symbol_properties_db_path) under
        the workspace.
    """
    bare = image_digest.split("@", 1)[-1]
    if not bare.startswith("sha256:"):
        raise MetadataStagingError(f"image_digest must be pinned, got {image_digest!r}")
    image_ref = f"{LEAN_IMAGE_REPO}@{bare}"

    cached = _stage_lean_metadata_from_cached_same_digest_workspace(workspace, bare)
    if cached is not None:
        return cached

    podman = shutil.which("podman")
    if not podman:
        if not allow_launcher_fallback:
            # The launcher (host) calls into this function with
            # ``allow_launcher_fallback=False`` so a misconfigured
            # launcher host (no podman) fails fast here instead of
            # recursively HTTP-POSTing /extract-metadata back into
            # itself. Codex-P2 / CodeRabbit-Major review-fix.
            raise MetadataStagingError(
                "podman is required but was not found on PATH "
                "(launcher fallback disabled; this branch is the "
                "launcher's own call into staging)"
            )
        # Data-plane container has no podman on PATH — by design, per
        # the launcher topology in lean-sidecar-lab.md §"Launcher
        # topology". Delegate to the host-side launcher via HTTP, then
        # verify the files landed in the workspace through our view of
        # the shared bind mount.
        return _stage_lean_metadata_via_launcher(workspace, image_digest)

    workspace.ensure_layout()
    workspace.data_dir.mkdir(parents=True, exist_ok=True)

    # Bounded podman subprocess timeouts: a hung podman would otherwise
    # stall the launch critical path indefinitely. 60s for all three —
    # on a healthy podman these are sub-second, but on a host with a
    # bloated overlay store, a cold first-use, or transient storage
    # contention we measured ``create`` at ~3.5s, ``cp`` of the LEAN
    # metadata files at ~6.5s each, and ``rm`` occasionally exceeding
    # 15s. The earlier 15/30s ceilings tripped the launcher into
    # ``MetadataStagingError`` and surfaced as a data-plane 502 even
    # though podman would have succeeded a few seconds later. 60s
    # absorbs that without making a truly-hung podman wait pathologically
    # long; the upstream data-plane HTTP timeout
    # (``launcher_client.py::_LAUNCH_HTTP_TIMEOUT_S``, currently 90s)
    # still bounds the round-trip.
    _CREATE_TIMEOUT_S = 60
    _CP_TIMEOUT_S = 60
    _RM_TIMEOUT_S = 60

    # ``podman create`` returns a container id we then ``cp`` out of.
    # We do not ``run`` it; nothing inside the image executes here.
    try:
        create = subprocess.run(
            [podman, "create", "--network=none", image_ref],
            capture_output=True,
            text=True,
            check=False,
            timeout=_CREATE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise MetadataStagingError(f"podman create timed out after {_CREATE_TIMEOUT_S}s") from e
    if create.returncode != 0:
        raise MetadataStagingError(f"podman create failed: {create.stderr.strip()}")
    container_id = create.stdout.strip()

    # MSYS_NO_PATHCONV prevents Git Bash on Windows from rewriting the
    # ``/Lean/Data/...`` container path into a host-side ``C:/...`` path
    # before podman ever sees it. The HOME / USERPROFILE env podman
    # needs to find its config still has to be present, so we copy the
    # whole environment and only add the path-mangling toggle.
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    # Pre-create the alternative dir so podman cp lands the
    # interest-rate subtree alongside the equity-only databases.
    (workspace.data_dir / "alternative").mkdir(parents=True, exist_ok=True)
    try:
        for src in (IMAGE_MARKET_HOURS, IMAGE_SYMBOL_PROPERTIES, IMAGE_INTEREST_RATE):
            # Interest rate lives under data/alternative/, the others
            # under data/. Pick the destination accordingly so the
            # extracted tree mirrors the image layout LEAN expects.
            dest = workspace.data_dir / "alternative" if src == IMAGE_INTEREST_RATE else workspace.data_dir
            try:
                cp = subprocess.run(
                    [
                        podman,
                        "cp",
                        f"{container_id}:{src}",
                        str(dest),
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                    timeout=_CP_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired as e:
                raise MetadataStagingError(f"podman cp {src} timed out after {_CP_TIMEOUT_S}s") from e
            if cp.returncode != 0:
                raise MetadataStagingError(f"podman cp {src} failed: {cp.stderr.strip()}")
    finally:
        # ``rm`` is best-effort cleanup; a timeout here leaves a
        # stopped container behind which an operator can garbage-
        # collect, so we do not raise.
        try:
            subprocess.run(
                [podman, "rm", container_id],
                capture_output=True,
                text=True,
                check=False,
                timeout=_RM_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            # Best-effort: a stopped container is recoverable manually.
            logger.warning(
                "podman rm %s timed out after %ss; container left behind",
                container_id,
                _RM_TIMEOUT_S,
            )

    mh, sp = list_metadata_databases(workspace)
    if mh is None or sp is None:
        raise MetadataStagingError(
            f"metadata databases not present in workspace after extract; market-hours={mh!r}, symbol-properties={sp!r}"
        )
    return mh, sp


def _stage_lean_metadata_from_cached_same_digest_workspace(
    workspace: Workspace,
    image_digest: str,
) -> tuple[Path, Path] | None:
    """Copy metadata from a previous workspace for the same LEAN digest.

    The image-bundled metadata is immutable for a pinned digest. Once a
    run has extracted it and recorded a manifest with the same
    ``lean_image_digest``, later workspaces can reuse those exact files
    without issuing another ``podman create``. This keeps the normal path
    independent of Podman's healthcheck/storage-lock state while still
    preserving the digest-scoped provenance contract.
    """
    artifacts_root = workspace.artifacts_root
    if not artifacts_root.exists():
        return None

    for manifest_path in sorted(artifacts_root.glob("*/manifest.json"), reverse=True):
        candidate_root = manifest_path.parent
        if candidate_root == workspace.root:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("skipping unreadable LEAN metadata cache manifest %s: %s", manifest_path, e)
            continue
        if manifest.get("lean_image_digest") != image_digest:
            continue

        source_data_dir = candidate_root / "workspace" / "data"
        source_mh = source_data_dir / "market-hours" / "market-hours-database.json"
        source_sp = source_data_dir / "symbol-properties" / "symbol-properties-database.csv"
        if not source_mh.exists() or not source_sp.exists():
            continue

        workspace.ensure_layout()
        workspace.data_dir.mkdir(parents=True, exist_ok=True)
        metadata_dirs = (
            Path("market-hours"),
            Path("symbol-properties"),
            Path("alternative") / "interest-rate",
        )
        try:
            for relative in metadata_dirs:
                source = source_data_dir / relative
                if not source.exists():
                    continue
                dest = workspace.data_dir / relative
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(source, dest)
        except (OSError, shutil.Error) as e:
            logger.warning(
                "skipping unreadable LEAN metadata cache workspace %s: %s",
                candidate_root,
                e,
            )
            for relative in metadata_dirs:
                shutil.rmtree(workspace.data_dir / relative, ignore_errors=True)
            continue

        mh, sp = list_metadata_databases(workspace)
        if mh is not None and sp is not None:
            logger.info(
                "reused LEAN metadata for %s from cached workspace %s",
                image_digest,
                candidate_root.name,
            )
            return mh, sp
    return None


def _stage_lean_metadata_via_launcher(
    workspace: Workspace,
    image_digest: str,
) -> tuple[Path, Path]:
    """Delegate metadata extraction to the launcher service.

    Used when the local environment has no ``podman`` on PATH — the
    production topology for the data-plane container. The launcher
    (a host process with podman) does the subprocess work and writes
    files to the workspace; we then re-read them through our view of
    the shared bind mount.

    The workspace's ``run_id`` is read from the dataclass field
    (``Workspace.run_id``) — the same value the data plane sent on the
    matching ``/launch`` request. Both endpoints address the same
    workspace by the same key without the data plane threading a
    separate id.
    """
    # Lazy import to keep the staging module importable in environments
    # where httpx is not installed (e.g., a minimal test fixture).
    from app.lean_sidecar.launcher_client import (
        LauncherClientError,
        post_extract_metadata_sync,
    )

    workspace.ensure_layout()
    workspace.data_dir.mkdir(parents=True, exist_ok=True)

    try:
        post_extract_metadata_sync(workspace.run_id, image_digest)
    except LauncherClientError as e:
        # Surface the launcher's failure as a MetadataStagingError so
        # callers handle this delegation path the same way they handle
        # the local-podman path. The message preserves the launcher's
        # reason label so an operator can distinguish in logs.
        raise MetadataStagingError(
            f"metadata extraction via launcher failed: {e}"
        ) from e

    mh, sp = list_metadata_databases(workspace)
    if mh is None or sp is None:
        raise MetadataStagingError(
            f"metadata databases not present in workspace after launcher "
            f"extract; market-hours={mh!r}, symbol-properties={sp!r}"
        )
    return mh, sp
