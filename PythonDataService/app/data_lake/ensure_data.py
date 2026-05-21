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
import time

from app.data_lake import fake_polygon
from app.data_lake.sessions import trading_sessions_for
from app.data_lake.types import (
    ArtifactFailure,
    ArtifactIdentity,
    ArtifactRecord,
    DataAvailabilityResult,
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


# Placeholder lean_data_root_path until Slice 1b wires the LEAN_DATA_ROOT env var.
# Slice 1b replaces this constant with settings.LEAN_DATA_ROOT.
_PLACEHOLDER_LEAN_DATA_ROOT = "/lean-data"


async def ensure_data(spec: DataRunSpec) -> DataAvailabilityResult:
    """Fixture-backed ensure_data (Slice 1a).

    Returns a canned DataAvailabilityResult. Known symbols (per fake_polygon)
    produce complete artifacts; unknown symbols produce per-artifact failures
    with reason='unknown_symbol'. No catalog writes, no Polygon calls.
    """
    started_ms = int(time.time() * 1000)
    required, non_sessions = expand_required_artifacts(spec)

    known = fake_polygon.known_symbols()
    artifacts: list[ArtifactRecord] = []
    failures: list[ArtifactFailure] = []

    for identity in required:
        if identity.symbol is None or identity.symbol in known:
            artifacts.append(fake_polygon.synth_artifact_record(identity))
        else:
            failures.append(
                ArtifactFailure(
                    artifact_kind=identity.artifact_kind,
                    symbol=identity.symbol,
                    trading_date=identity.trading_date,
                    data_type=identity.data_type,
                    reason="unknown_symbol",
                    detail=f"symbol {identity.symbol!r} not in Slice 1a stub set",
                    attempt_count=1,
                )
            )

    if failures and artifacts:
        overall_status = "partial"
    elif failures:
        overall_status = "failed"
    else:
        overall_status = "complete"

    completed_ms = int(time.time() * 1000)
    return DataAvailabilityResult(
        request_id=spec.request_id,
        overall_status=overall_status,
        lean_data_root_path=_PLACEHOLDER_LEAN_DATA_ROOT,
        data_availability_hash=_compute_data_availability_hash(artifacts),
        artifacts=artifacts,
        failures=failures,
        skipped_non_sessions=non_sessions,
        fetched_artifact_count=0,
        reused_artifact_count=len(artifacts),
        refreshed_artifact_count=0,
        completed_at_ms=completed_ms,
        duration_ms=completed_ms - started_ms,
    )
