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

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.engine.data.lean_format import write_lean_day_zip
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.lean_config import LeanConfig
from app.lean_sidecar.workspace import Workspace


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
    """
    workspace.ensure_layout()
    written: list[Path] = []
    for trading_date, bars in bars_by_date:
        path = write_lean_day_zip(
            workspace.data_dir,
            symbol,
            trading_date,
            bars,
        )
        written.append(path)
    return tuple(written)


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
