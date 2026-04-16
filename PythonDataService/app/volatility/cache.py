"""
Surface Cache Module
====================

Disk-backed surface cache with deterministic IDs.

Cache structure:
├── surfaces/{surface_id}.meta.json
├── grids/{surface_id}.parquet
├── smiles/{surface_id}.json
└── diagnostics/{surface_id}.json
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import QuantLib

from app.volatility.conventions import SurfaceConventions

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class DataFilters:
    """Filters applied to raw option data before surface building."""

    min_dte: int = 7
    max_dte: int = 365
    min_open_interest: int = 10
    max_spread_pct: float = 0.20


def compute_surface_id(
    ticker: str,
    date: str,
    method: str,
    conventions: SurfaceConventions,
    filters: DataFilters,
    n_options: int,
) -> str:
    """
    Compute deterministic surface ID from all build inputs.

    Creates SHA-256 hash of canonical representation; returns first 20 hex chars.

    Args:
        ticker: Underlying ticker symbol
        date: Evaluation date (YYYY-MM-DD)
        method: Surface fitting method (variance, sabr, svi)
        conventions: SurfaceConventions instance
        filters: DataFilters instance
        n_options: Number of option records included in surface

    Returns:
        20-character hex string (first 20 chars of SHA-256)
    """
    hash_input = {
        "ticker": ticker,
        "date": date,
        "method": method,
        "conventions": conventions.to_hash_dict(),
        "filters": {
            "min_dte": filters.min_dte,
            "max_dte": filters.max_dte,
            "min_open_interest": filters.min_open_interest,
            "max_spread_pct": filters.max_spread_pct,
        },
        "n_options": n_options,
    }

    hash_bytes = hashlib.sha256(json.dumps(hash_input, sort_keys=True).encode("utf-8")).digest()
    hash_hex = hash_bytes.hex()

    return hash_hex[:20]


class SurfaceCache:
    """
    Disk-backed surface cache with JSON/Parquet storage.

    Manages reading/writing surface metadata, grids, smiles, and diagnostics
    with automatic schema validation.
    """

    def __init__(self, cache_dir: str = "cache") -> None:
        """
        Initialize cache manager.

        Args:
            cache_dir: Root cache directory (default: "cache")
        """
        self.cache_dir = Path(cache_dir)
        self.surfaces_dir = self.cache_dir / "surfaces"
        self.grids_dir = self.cache_dir / "grids"
        self.smiles_dir = self.cache_dir / "smiles"
        self.diagnostics_dir = self.cache_dir / "diagnostics"

        for directory in [
            self.surfaces_dir,
            self.grids_dir,
            self.smiles_dir,
            self.diagnostics_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)

    def exists(self, surface_id: str) -> bool:
        """
        Check if surface exists in cache.

        Args:
            surface_id: Surface ID

        Returns:
            True if all required files exist
        """
        meta_path = self.surfaces_dir / f"{surface_id}.meta.json"
        grid_path = self.grids_dir / f"{surface_id}.parquet"
        smiles_path = self.smiles_dir / f"{surface_id}.json"
        diagnostics_path = self.diagnostics_dir / f"{surface_id}.json"

        return all(p.exists() for p in [meta_path, grid_path, smiles_path, diagnostics_path])

    def is_valid(self, surface_id: str) -> bool:
        """
        Check if surface exists AND schema_version matches.

        Args:
            surface_id: Surface ID

        Returns:
            True if surface exists and schema is current
        """
        if not self.exists(surface_id):
            return False

        meta = self.read_meta(surface_id)
        if meta is None:
            return False

        if meta.get("schema_version") != SCHEMA_VERSION:
            logger.warning(
                f"Surface {surface_id} has incompatible schema_version: "
                f"{meta.get('schema_version')} != {SCHEMA_VERSION}"
            )
            return False

        return True

    def write_meta(self, surface_id: str, meta: dict[str, Any]) -> None:
        """
        Write surface metadata as JSON.

        Automatically adds schema_version, quantlib_version, and built_at.

        Args:
            surface_id: Surface ID
            meta: Metadata dict
        """
        meta_with_info = {
            "schema_version": SCHEMA_VERSION,
            "quantlib_version": QuantLib.__version__,
            "built_at": datetime.utcnow().isoformat() + "Z",
            **meta,
        }

        meta_path = self.surfaces_dir / f"{surface_id}.meta.json"
        with open(meta_path, "w") as f:
            json.dump(meta_with_info, f, indent=2)

        logger.debug(f"Wrote surface meta to {meta_path}")

    def write_grid(self, surface_id: str, grid: dict[str, Any]) -> None:
        """
        Write surface grid as Parquet.

        Converts grid dict to DataFrame and saves.

        Args:
            surface_id: Surface ID
            grid: Grid data (dict with 'strikes', 'ttms', 'ivs', etc.)
        """
        grid_path = self.grids_dir / f"{surface_id}.parquet"

        df = pd.DataFrame(grid)
        df.to_parquet(grid_path, index=False, compression="snappy")

        logger.debug(f"Wrote surface grid to {grid_path}")

    def write_smiles(self, surface_id: str, smiles: dict[str, Any]) -> None:
        """
        Write fitted smiles as JSON.

        Args:
            surface_id: Surface ID
            smiles: Smiles data (typically keyed by TTM)
        """
        smiles_path = self.smiles_dir / f"{surface_id}.json"
        with open(smiles_path, "w") as f:
            json.dump(smiles, f, indent=2)

        logger.debug(f"Wrote smiles to {smiles_path}")

    def write_diagnostics(self, surface_id: str, diagnostics: dict[str, Any]) -> None:
        """
        Write surface diagnostics as JSON.

        Args:
            surface_id: Surface ID
            diagnostics: Diagnostics data
        """
        diagnostics_path = self.diagnostics_dir / f"{surface_id}.json"
        with open(diagnostics_path, "w") as f:
            json.dump(diagnostics, f, indent=2)

        logger.debug(f"Wrote diagnostics to {diagnostics_path}")

    def read_meta(self, surface_id: str) -> dict[str, Any] | None:
        """
        Read surface metadata from JSON.

        Args:
            surface_id: Surface ID

        Returns:
            Metadata dict, or None if file does not exist
        """
        meta_path = self.surfaces_dir / f"{surface_id}.meta.json"
        if not meta_path.exists():
            logger.warning(f"Meta file not found: {meta_path}")
            return None

        try:
            with open(meta_path) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read meta from {meta_path}: {e}")
            return None

    def read_grid(self, surface_id: str) -> dict[str, Any] | None:
        """
        Read surface grid from Parquet.

        Args:
            surface_id: Surface ID

        Returns:
            Grid dict (converted from DataFrame), or None if file does not exist
        """
        grid_path = self.grids_dir / f"{surface_id}.parquet"
        if not grid_path.exists():
            logger.warning(f"Grid file not found: {grid_path}")
            return None

        try:
            df = pd.read_parquet(grid_path)
            return df.to_dict(orient="list")
        except Exception as e:
            logger.error(f"Failed to read grid from {grid_path}: {e}")
            return None

    def read_smiles(self, surface_id: str) -> dict[str, Any] | None:
        """
        Read fitted smiles from JSON.

        Args:
            surface_id: Surface ID

        Returns:
            Smiles dict, or None if file does not exist
        """
        smiles_path = self.smiles_dir / f"{surface_id}.json"
        if not smiles_path.exists():
            logger.warning(f"Smiles file not found: {smiles_path}")
            return None

        try:
            with open(smiles_path) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read smiles from {smiles_path}: {e}")
            return None

    def read_diagnostics(self, surface_id: str) -> dict[str, Any] | None:
        """
        Read surface diagnostics from JSON.

        Args:
            surface_id: Surface ID

        Returns:
            Diagnostics dict, or None if file does not exist
        """
        diagnostics_path = self.diagnostics_dir / f"{surface_id}.json"
        if not diagnostics_path.exists():
            logger.warning(f"Diagnostics file not found: {diagnostics_path}")
            return None

        try:
            with open(diagnostics_path) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read diagnostics from {diagnostics_path}: {e}")
            return None
