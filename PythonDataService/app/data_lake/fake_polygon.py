"""Slice 1a fixture-backed Polygon stub.

ensure_data calls this instead of the real Polygon fetcher in Slice 1a so the
HTTP boundary and Pydantic round-trip can be tested without a Polygon API key
or fixture management overhead. Real fetcher lands in Slice 1b.

ABSOLUTELY NOT used in production. Guarded by DATA_LAKE_ENABLED at the route
layer; even with the flag on, this stub returns synthetic data with
file_sha256='0'*64 — clearly non-real bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.data_lake.path_policy import (
    LeanDailyBarPath,
    LeanFactorFilePath,
    LeanMapFilePath,
    LeanMinuteBarPath,
)
from app.data_lake.types import ArtifactIdentity, ArtifactRecord

_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests/fixtures/data_lake_skeleton/canned_response.json"
_ZERO_SHA = "0" * 64
_ZERO_HASH = "0" * 64


def known_symbols() -> set[str]:
    with _FIXTURE_PATH.open("r", encoding="utf-8") as f:
        return set(json.load(f)["known_symbols"])


def synth_artifact_record(identity: ArtifactIdentity) -> ArtifactRecord:
    """Defensive boundary: every artifact kind now has a real implementation.

    Slice 1c wired all artifact kinds (factor_file, map_file, metadata,
    minute-quote, daily-trade, minute-trade) through real implementations in
    ensure_data. This stub must never be called; if it fires the dispatch logic
    in ensure_data.py has regressed.

    The module remains in Slice 1c as a defensive boundary.
    Slice 1d deletes the module entirely.
    """
    raise NotImplementedError(
        "fake_polygon is retired in Slice 1c; all kinds now have real "
        f"implementations. If this fires, ensure_data dispatch logic is wrong "
        f"for identity: {identity!r}"
    )


def _path_for(identity: ArtifactIdentity) -> str:
    if identity.artifact_kind == "time_series_bars":
        if identity.resolution == "minute":
            return str(
                LeanMinuteBarPath(
                    market=identity.market,  # type: ignore[arg-type]
                    symbol=identity.symbol or "",
                    trading_date=identity.trading_date,  # type: ignore[arg-type]
                    data_type=identity.data_type,  # type: ignore[arg-type]
                ).relative_path()
            )
        if identity.resolution == "daily":
            return str(
                LeanDailyBarPath(
                    market=identity.market,  # type: ignore[arg-type]
                    symbol=identity.symbol or "",
                ).relative_path()
            )
    if identity.artifact_kind == "factor_file":
        return str(
            LeanFactorFilePath(
                market=identity.market,  # type: ignore[arg-type]
                symbol=identity.symbol or "",
            ).relative_path()
        )
    if identity.artifact_kind == "map_file":
        return str(
            LeanMapFilePath(
                market=identity.market,  # type: ignore[arg-type]
                symbol=identity.symbol or "",
            ).relative_path()
        )
    if identity.artifact_kind == "metadata":
        # Metadata staging lands in Slice 1c. expand_required_artifacts does not
        # emit metadata artifacts in Slice 1a, so this branch should never be reached.
        raise NotImplementedError("metadata staging lands in Slice 1c")
    raise ValueError(f"unsupported artifact_kind in fake stub: {identity.artifact_kind!r}")
