"""Polygon -> LEAN data lake -- ensure_data orchestration.

Slice 1a: fixture-backed canned responses; no real Polygon, no catalog INSERT,
no atomic writes. Sufficient to exercise the HTTP boundary, the Pydantic
contract, and the session-expansion logic end-to-end.

Real Polygon fetching + atomic writes + leases land in Slice 1b.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4
"""

from __future__ import annotations

import hashlib
import json
import logging

from app.data_lake.sessions import trading_sessions_for
from app.data_lake.types import (
    ArtifactIdentity,
    ArtifactRecord,
    DataRunSpec,
    NonSessionRecord,
)

logger = logging.getLogger(__name__)


def expand_required_artifacts(
    spec: DataRunSpec,
) -> tuple[list[ArtifactIdentity], list[NonSessionRecord]]:
    """Compute the list of artifacts the spec requires and the calendar gaps it skips.

    Order of the returned list is deterministic so two ensure_data calls with
    the same spec produce the same data_availability_hash.

    LEAN metadata is NOT staged here. It is an unconditional Phase 0 prerequisite
    staged in a separate step before this function is called (Slice 1c). In Slice
    1a no metadata artifacts are produced.
    """
    sessions, non_sessions = trading_sessions_for(spec.market, spec.start_trading_date, spec.end_trading_date)
    required: list[ArtifactIdentity] = []

    for symbol in sorted(spec.symbols):
        # Per-day minute bars.
        for trading_date in sessions:
            for data_type in spec.data_types:
                provider = "polygon" if data_type == "trade" else "learn_ai_derived"
                required.append(
                    ArtifactIdentity(
                        artifact_kind="time_series_bars",
                        market=spec.market,
                        symbol=symbol,
                        trading_date=trading_date,
                        resolution="minute",
                        data_type=data_type,
                        provider=provider,
                        price_adjustment_mode="raw",
                    )
                )

        # Corp-action artifacts.
        if spec.include_factor_files:
            required.append(
                ArtifactIdentity(
                    artifact_kind="factor_file",
                    market=spec.market,
                    symbol=symbol,
                    provider="polygon",
                    price_adjustment_mode="raw",
                )
            )
        if spec.include_map_files:
            required.append(
                ArtifactIdentity(
                    artifact_kind="map_file",
                    market=spec.market,
                    symbol=symbol,
                    provider="polygon",
                    price_adjustment_mode="raw",
                )
            )

        # Daily-trade derived artifact (per symbol, null trading_date).
        if "trade" in spec.data_types:
            required.append(
                ArtifactIdentity(
                    artifact_kind="time_series_bars",
                    market=spec.market,
                    symbol=symbol,
                    trading_date=None,
                    resolution="daily",
                    data_type="trade",
                    provider="learn_ai_derived",
                    price_adjustment_mode="raw",
                )
            )

    return required, non_sessions


def _compute_data_availability_hash(artifacts: list[ArtifactRecord]) -> str:
    """sha256 over a sorted byte-AND-contract tuple per artifact."""
    fingerprints: list[tuple] = []
    for a in artifacts:
        fingerprints.append(
            (
                a.artifact_kind,
                a.market,
                a.symbol,
                a.trading_date.isoformat() if a.trading_date else None,
                a.data_type,
                a.file_path,
                a.file_sha256,
                a.row_count,
                a.first_bar_start_ms,
                a.last_bar_start_ms,
            )
        )
    fingerprints.sort(key=lambda t: tuple("" if v is None else str(v) for v in t))
    blob = json.dumps(fingerprints, default=str, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ensure_data() itself is filled in by Task 16 after fake_polygon is in place.
