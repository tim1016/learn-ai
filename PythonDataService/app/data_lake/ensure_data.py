"""Polygon -> LEAN data lake -- ensure_data orchestration.

Slice 1a: fixture-backed canned responses; no real Polygon, no catalog INSERT,
no atomic writes. Sufficient to exercise the HTTP boundary, the Pydantic
contract, and the session-expansion logic end-to-end.

Slice 1b: dispatch by artifact kind. Minute-trade artifacts now flow through
the real Polygon → atomic-write → catalog-claim cycle. Other artifact kinds
(factor / map / daily / quote / metadata) keep the Slice 1a fake_polygon stub
until Slice 1c.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings
from app.data_lake import catalog_client, fake_polygon
from app.data_lake.atomic import atomic_write_and_promote
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.path_policy import LeanMinuteBarPath
from app.data_lake.polygon_fetcher import (
    PolygonAuthError,
    PolygonBar,
    PolygonEntitlementError,
    PolygonFetchError,
    PolygonRateLimitedError,
    PolygonUnknownSymbolError,
    fetch_minute_trade_aggregates,
)
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

_ET = ZoneInfo("America/New_York")
_WORKER_ID = os.environ.get("HOSTNAME", "py-data-lake")  # one writer per process
_LEASE_TTL_MS = 300_000


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


def _is_minute_trade(identity: ArtifactIdentity) -> bool:
    return (
        identity.artifact_kind == "time_series_bars"
        and identity.resolution == "minute"
        and identity.data_type == "trade"
    )


def _polygon_bar_to_minute_trade_bar(pb: PolygonBar) -> MinuteTradeBar:
    bar_start_utc = datetime.fromtimestamp(pb.t_ms / 1000, tz=ZoneInfo("UTC"))
    return MinuteTradeBar(
        bar_start_et=bar_start_utc.astimezone(_ET),
        open=Decimal(str(pb.open)),
        high=Decimal(str(pb.high)),
        low=Decimal(str(pb.low)),
        close=Decimal(str(pb.close)),
        volume=pb.volume,
    )


async def _process_minute_trade_artifact(
    identity: ArtifactIdentity,
    spec: DataRunSpec,
) -> tuple[ArtifactRecord | None, ArtifactFailure | None]:
    """Claim → fetch → write → complete one minute-trade artifact.

    Returns (record, None) on success or (None, failure) on error.
    """
    rel_path = LeanMinuteBarPath(
        market=identity.market,  # type: ignore[arg-type]
        symbol=identity.symbol or "",
        trading_date=identity.trading_date,  # type: ignore[arg-type]
        data_type="trade",
    ).relative_path()
    file_path = str(rel_path)

    # data_contract_hash is a placeholder for Slice 1b.
    # Slice 1c computes it deterministically over canonical(provider_params,
    # price_adjustment_mode, session_policy). The unique constraint already
    # enforces (market, symbol, date, data_type, provider, price_adjustment_mode)
    # uniqueness; this hash is for forward-compat fingerprinting.
    data_contract_hash = "x" * 64

    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        data_contract_hash=data_contract_hash,
        file_path=file_path,
    )
    if artifact_id is None:
        # Already complete (or in-flight); read the existing complete row.
        existing = await catalog_client.select_coverage_minute_bars(
            market=identity.market,  # type: ignore[arg-type]
            symbol=identity.symbol,  # type: ignore[arg-type]
            data_type="trade",
            start_trading_date=identity.trading_date,  # type: ignore[arg-type]
            end_trading_date=identity.trading_date,  # type: ignore[arg-type]
        )
        if existing:
            return existing[0], None
        # In-flight elsewhere; Slice 1b doesn't poll. Report as transient.
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind,
            symbol=identity.symbol,
            trading_date=identity.trading_date,
            data_type=identity.data_type,
            reason="lease_timeout",
            detail="another worker has the lease; polling not implemented in Slice 1b",
            attempt_count=1,
        )

    # Fetch from Polygon.
    api_key = settings.POLYGON_API_KEY
    try:
        polygon_bars = await fetch_minute_trade_aggregates(
            symbol=identity.symbol or "",
            start=identity.trading_date,  # type: ignore[arg-type]
            end=identity.trading_date,  # type: ignore[arg-type]
            api_key=api_key,
        )
    except PolygonAuthError as e:
        await catalog_client.fail_artifact(artifact_id, "provider_auth_error", str(e))
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind,
            symbol=identity.symbol,
            trading_date=identity.trading_date,
            data_type=identity.data_type,
            reason="provider_auth_error",
            detail=str(e),
            attempt_count=1,
        )
    except PolygonEntitlementError as e:
        await catalog_client.fail_artifact(artifact_id, "provider_entitlement_error", str(e))
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind,
            symbol=identity.symbol,
            trading_date=identity.trading_date,
            data_type=identity.data_type,
            reason="provider_entitlement_error",
            detail=str(e),
            attempt_count=1,
        )
    except PolygonRateLimitedError as e:
        await catalog_client.fail_artifact(artifact_id, "provider_rate_limited", str(e))
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind,
            symbol=identity.symbol,
            trading_date=identity.trading_date,
            data_type=identity.data_type,
            reason="provider_rate_limited",
            detail=str(e),
            attempt_count=1,
        )
    except PolygonUnknownSymbolError as e:
        await catalog_client.fail_artifact(artifact_id, "unknown_symbol", str(e))
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind,
            symbol=identity.symbol,
            trading_date=identity.trading_date,
            data_type=identity.data_type,
            reason="unknown_symbol",
            detail=str(e),
            attempt_count=1,
        )
    except PolygonFetchError as e:
        await catalog_client.fail_artifact(artifact_id, "provider_api_error", str(e))
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind,
            symbol=identity.symbol,
            trading_date=identity.trading_date,
            data_type=identity.data_type,
            reason="provider_api_error",
            detail=str(e),
            attempt_count=1,
        )

    if not polygon_bars:
        await catalog_client.fail_artifact(artifact_id, "provider_no_data", "empty response")
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind,
            symbol=identity.symbol,
            trading_date=identity.trading_date,
            data_type=identity.data_type,
            reason="provider_no_data",
            detail="Polygon returned no bars",
            attempt_count=1,
        )

    # Convert + encode + write.
    minute_bars = [_polygon_bar_to_minute_trade_bar(b) for b in polygon_bars]
    payload = build_minute_trade_zip_bytes(
        symbol=identity.symbol or "",
        trading_date_yyyymmdd=identity.trading_date.strftime("%Y%m%d"),  # type: ignore[union-attr]
        bars=minute_bars,
    )
    lake_root = Path(settings.LEAN_DATA_WRITE_ROOT) / "lake"
    staging_root = Path(settings.LEAN_DATA_WRITE_ROOT) / "staging"
    file_sha = atomic_write_and_promote(
        content=payload,
        lake_root=lake_root,
        staging_root=staging_root,
        rel_lake_path=rel_path,
        request_id=spec.request_id,
        worker_id=_WORKER_ID,
        attempt=1,
    )

    first_bar_ms = polygon_bars[0].t_ms
    last_bar_ms = polygon_bars[-1].t_ms
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=len(polygon_bars),
        first_bar_start_ms=first_bar_ms,
        last_bar_start_ms=last_bar_ms,
        file_size_bytes=len(payload),
        file_sha256=file_sha,
    )

    return (
        ArtifactRecord(
            id=artifact_id,
            artifact_kind=identity.artifact_kind,
            market=identity.market,
            symbol=identity.symbol,
            trading_date=identity.trading_date,
            resolution=identity.resolution,
            data_type=identity.data_type,
            provider=identity.provider,
            price_adjustment_mode=identity.price_adjustment_mode,
            data_contract_hash=data_contract_hash,
            file_path=file_path,
            file_sha256=file_sha,
            row_count=len(polygon_bars),
            first_bar_start_ms=first_bar_ms,
            last_bar_start_ms=last_bar_ms,
        ),
        None,
    )


async def ensure_data(spec: DataRunSpec) -> DataAvailabilityResult:
    """Dispatch by artifact kind: minute-trade through real pipeline; others
    keep the Slice 1a fake_polygon stub behavior.

    Slice 1c replaces the stub paths with real implementations (factor / map /
    derived daily / quote / metadata).
    """
    started_ms = int(time.time() * 1000)
    required, non_sessions = expand_required_artifacts(spec)

    # Ensure pool exists. init_pool is idempotent; pool stays alive across calls.
    await catalog_client.init_pool()

    artifacts: list[ArtifactRecord] = []
    failures: list[ArtifactFailure] = []
    fetched_count = 0
    reused_count = 0

    for identity in required:
        if _is_minute_trade(identity):
            record, failure = await _process_minute_trade_artifact(identity, spec)
            if record is not None:
                artifacts.append(record)
                # Heuristic: treat each successful minute-trade record as fetched=1.
                # Precise cache-hit tracking (fetched vs reused) lands in Slice 1d
                # when ensure_data does a coverage SELECT before claim.
                fetched_count += 1
            elif failure is not None:
                failures.append(failure)
        else:
            # Non-minute-trade: keep Slice 1a stub behavior.
            # (factor / map / daily / metadata implementations land in Slice 1c.)
            try:
                artifacts.append(fake_polygon.synth_artifact_record(identity))
                reused_count += 1
            except ValueError as exc:
                # Defensive: synth_artifact_record refuses minute-trade — if a
                # dispatch bug routed minute-trade here, that's the error.
                failures.append(
                    ArtifactFailure(
                        artifact_kind=identity.artifact_kind,
                        symbol=identity.symbol,
                        trading_date=identity.trading_date,
                        data_type=identity.data_type,
                        reason="internal_error",
                        detail=str(exc),
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
        lean_data_root_path=str(Path(settings.LEAN_DATA_WRITE_ROOT) / "lake"),
        data_availability_hash=_compute_data_availability_hash(artifacts),
        artifacts=artifacts,
        failures=failures,
        skipped_non_sessions=non_sessions,
        fetched_artifact_count=fetched_count,
        reused_artifact_count=reused_count,
        refreshed_artifact_count=0,
        completed_at_ms=completed_ms,
        duration_ms=completed_ms - started_ms,
    )
