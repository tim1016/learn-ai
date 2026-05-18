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

    Args:
        workspace: Resolved workspace; ``data_dir`` is created if missing.
        image_digest: ``sha256:...`` (or full ``repo@sha256:...``) to extract
            from. Must already be pulled locally; this helper does not pull.

    Returns:
        Tuple of (market_hours_db_path, symbol_properties_db_path) under
        the workspace.
    """
    bare = image_digest.split("@", 1)[-1]
    if not bare.startswith("sha256:"):
        raise MetadataStagingError(f"image_digest must be pinned, got {image_digest!r}")
    image_ref = f"{LEAN_IMAGE_REPO}@{bare}"

    podman = shutil.which("podman")
    if not podman:
        raise MetadataStagingError("podman is required but was not found on PATH")

    workspace.ensure_layout()
    workspace.data_dir.mkdir(parents=True, exist_ok=True)

    # Bounded podman subprocess timeouts: a hung podman would otherwise
    # stall the launch critical path indefinitely. 30s for create+cp
    # (a hot pull is sub-second; 30s is generous), 15s for rm.
    _CREATE_TIMEOUT_S = 30
    _CP_TIMEOUT_S = 30
    _RM_TIMEOUT_S = 15

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
