"""Polygon -> LEAN data lake -- ensure_data orchestration.

Slice 1a: fixture-backed canned responses; no real Polygon, no catalog INSERT,
no atomic writes. Sufficient to exercise the HTTP boundary, the Pydantic
contract, and the session-expansion logic end-to-end.

Slice 1b: dispatch by artifact kind. Minute-trade artifacts now flow through
the real Polygon → atomic-write → catalog-claim cycle. Other artifact kinds
(factor / map / daily / quote / metadata) keep the Slice 1a fake_polygon stub
until Slice 1c.

Slice 1c: all artifact kinds have real implementations. Phase 0 metadata
bootstrap (LEAN image extraction), Pass 1 (Polygon-sourced: minute-trade,
factor_file, map_file), Pass 2 (derived: minute-quote, daily-trade). Real
data_contract_hash replaces the 'x' * 64 placeholder. fake_polygon is
retired as a defensive boundary.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.atomic import atomic_write_and_promote
from app.data_lake.data_contract import data_contract_hash as _dch
from app.data_lake.derived_daily import (
    aggregate_minute_to_daily,
    build_daily_zip_bytes,
    rth_daily_closes,
)
from app.data_lake.derived_quote import build_minute_quote_zip_bytes
from app.data_lake.factor_files import FactorFileReferenceError, build_factor_file_bytes
from app.data_lake.lean_metadata import LeanMetadataExtractionError, extract_lean_metadata
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.map_files import build_map_file_bytes
from app.data_lake.path_policy import (
    LeanDailyBarPath,
    LeanFactorFilePath,
    LeanMapFilePath,
    LeanMetadataPath,
    LeanMinuteBarPath,
)
from app.data_lake.polygon_corp_actions import fetch_dividends, fetch_splits
from app.data_lake.polygon_fetcher import (
    PolygonAuthError,
    PolygonBar,
    PolygonEntitlementError,
    PolygonFetchError,
    PolygonRateLimitedError,
    PolygonUnknownSymbolError,
    fetch_minute_trade_aggregates,
)
from app.data_lake.polygon_ticker_events import fetch_ticker_events
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

# data_contract_hash provider params (canonical per artifact kind)
_DCH_MINUTE_TRADE_PARAMS = {
    "adjusted": False,
    "timespan": "minute",
    "multiplier": 1,
    "endpoint": "v2/aggs",
}
_DCH_FACTOR_FILE_PARAMS = {
    "endpoints": ["v3/reference/splits", "v3/reference/dividends"],
}
_DCH_MAP_FILE_PARAMS = {
    "endpoint": "v3/reference/tickers/{sym}/events",
}


def _minute_trade_dch() -> str:
    return _dch(
        provider="polygon",
        provider_params=_DCH_MINUTE_TRADE_PARAMS,
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )


def _factor_file_dch(history_start: date, history_end: date) -> str:
    """Factor-file contract hash includes the history window.

    The factor file content includes anchor rows at history_start and
    history_end, so two calls with different windows produce different
    file content. Including the window prevents cache poisoning where a
    narrower-window file is returned for a wider-window request.
    """
    return _dch(
        provider="polygon",
        provider_params={
            **_DCH_FACTOR_FILE_PARAMS,
            "history_start": history_start.isoformat(),
            "history_end": history_end.isoformat(),
        },
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )


def _map_file_dch() -> str:
    return _dch(
        provider="polygon",
        provider_params=_DCH_MAP_FILE_PARAMS,
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )


def _metadata_dch(lean_image_digest: str, file_name: str) -> str:
    return _dch(
        provider="lean_image_extract",
        provider_params={"lean_image_digest": lean_image_digest, "file_name": file_name},
        price_adjustment_mode=None,
        session_policy="full",
        lean_format_version=1,
    )


def _quote_dch(source_artifact_id: int, source_file_sha256: str) -> str:
    return _dch(
        provider="learn_ai_derived",
        provider_params={
            "source": "minute-trade",
            "source_artifact_id": source_artifact_id,
            "source_file_sha256": source_file_sha256,
        },
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )


def _daily_dch(source_artifact_ids: list[int], source_file_sha256s: list[str]) -> str:
    return _dch(
        provider="learn_ai_derived",
        provider_params={
            "source": "minute-trade",
            "source_artifact_ids": sorted(source_artifact_ids),
            "source_file_sha256s": sorted(source_file_sha256s),
        },
        price_adjustment_mode="raw",
        session_policy="full",
        lean_format_version=1,
    )


def expand_required_artifacts(
    spec: DataRunSpec,
    market_hours_db_path: Path | None = None,
) -> tuple[list[ArtifactIdentity], list[NonSessionRecord]]:
    """Compute the list of artifacts the spec requires and the calendar gaps it skips.

    Order of the returned list is deterministic so two ensure_data calls with
    the same spec produce the same data_availability_hash.

    LEAN metadata is NOT staged here. It is an unconditional Phase 0 prerequisite
    staged in a separate step before this function is called (Slice 1c). In Slice
    1a no metadata artifacts are produced.
    """
    sessions, non_sessions = trading_sessions_for(
        spec.market,
        spec.start_trading_date,
        spec.end_trading_date,
        market_hours_db_path=market_hours_db_path,
    )
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


def _is_minute_quote(identity: ArtifactIdentity) -> bool:
    return (
        identity.artifact_kind == "time_series_bars"
        and identity.resolution == "minute"
        and identity.data_type == "quote"
    )


def _is_daily_trade(identity: ArtifactIdentity) -> bool:
    return (
        identity.artifact_kind == "time_series_bars"
        and identity.resolution == "daily"
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


def _lake_roots(spec: DataRunSpec) -> tuple[Path, Path]:
    """Return (lake_root, staging_root) for the current spec."""
    write_root = Path(settings.LEAN_DATA_WRITE_ROOT)
    return write_root / "lake", write_root / "staging"


def _read_minute_trade_bars(file_path: str, lake_root: Path) -> list[MinuteTradeBar]:
    """Read a complete minute-trade artifact from disk and reconstruct MinuteTradeBar list.

    The zip contains one CSV: <yyyymmdd>_<sym>_minute_trade.csv. Each row:
      ms_since_midnight_et, open*10000, high*10000, low*10000, close*10000, volume

    The trading date is inferred from the file path (equity/<mkt>/minute/<sym>/<yyyymmdd>_trade.zip).
    """
    full_path = lake_root / Path(*PurePosixPath(file_path).parts)
    with zipfile.ZipFile(full_path) as zf:
        names = zf.namelist()
        if not names:
            return []
        csv_bytes = zf.read(names[0])

    # Parse the date and symbol from the CSV filename: <yyyymmdd>_<sym>_minute_trade.csv
    csv_name = names[0]
    date_part = csv_name[:8]
    trading_year = int(date_part[:4])
    trading_month = int(date_part[4:6])
    trading_day = int(date_part[6:8])

    bars: list[MinuteTradeBar] = []
    for line in csv_bytes.decode("ascii").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        ms_since_midnight = int(parts[0])
        open_dc = int(parts[1])
        high_dc = int(parts[2])
        low_dc = int(parts[3])
        close_dc = int(parts[4])
        volume = int(parts[5])

        # Reconstruct bar_start_et from ms_since_midnight and the trading date.
        hours = ms_since_midnight // 3_600_000
        minutes = (ms_since_midnight % 3_600_000) // 60_000
        bar_start_et = datetime(
            trading_year,
            trading_month,
            trading_day,
            hours,
            minutes,
            0,
            tzinfo=_ET,
        )
        bars.append(
            MinuteTradeBar(
                bar_start_et=bar_start_et,
                open=Decimal(open_dc) / Decimal(10_000),
                high=Decimal(high_dc) / Decimal(10_000),
                low=Decimal(low_dc) / Decimal(10_000),
                close=Decimal(close_dc) / Decimal(10_000),
                volume=volume,
            )
        )
    return bars


# ---------------------------------------------------------------------------
# Phase 0: LEAN metadata bootstrap
# ---------------------------------------------------------------------------


async def _bootstrap_metadata_artifact(
    file_name: str,
    metadata_kind: str,
    rel_path: PurePosixPath,
    lean_image_digest: str,
    spec: DataRunSpec,
    lake_root: Path,
    staging_root: Path,
) -> tuple[ArtifactRecord | None, bool]:
    """Claim, extract, write, and complete one LEAN metadata artifact.

    Returns (record, is_reused). is_reused=True when the artifact already existed
    in the catalog (cache hit). Returns (None, False) when in-flight elsewhere or
    extraction failed.
    """
    dch = _metadata_dch(lean_image_digest, file_name)
    file_path = str(rel_path)

    identity = ArtifactIdentity(
        artifact_kind="metadata",
        market=spec.market,
        symbol=None,
        provider="lean_image_extract",
        price_adjustment_mode=None,
    )

    artifact_id = await catalog_client.claim_metadata_artifact(
        identity=identity,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        data_contract_hash=dch,
        file_path=file_path,
    )

    if artifact_id is None:
        # Already exists (complete or in-flight). Try to return the complete row.
        existing = await catalog_client.select_complete_metadata_artifact(dch)
        if existing is not None:
            return existing, True  # cache hit
        # In-flight elsewhere; skip (non-blocking in Slice 1c).
        logger.warning(
            "data_lake.ensure_data: metadata artifact %s is in-flight elsewhere; "
            "Phase 0 may use fallback holiday list for sessions.",
            file_name,
        )
        return None, False

    # Fetch from launcher.
    try:
        mh_bytes, sp_bytes = await extract_lean_metadata(
            image_digest=lean_image_digest,
            launcher_url=settings.LEAN_LAUNCHER_URL,
            launcher_token=settings.LEAN_LAUNCHER_TOKEN,
        )
    except LeanMetadataExtractionError as e:
        await catalog_client.fail_artifact(artifact_id, "provider_api_error", str(e))
        logger.warning("data_lake.ensure_data: metadata extraction failed: %s", e)
        return None, False

    content = mh_bytes if metadata_kind == "market_hours" else sp_bytes
    file_sha = atomic_write_and_promote(
        content=content,
        lake_root=lake_root,
        staging_root=staging_root,
        rel_lake_path=rel_path,
        request_id=spec.request_id,
        worker_id=_WORKER_ID,
        attempt=1,
    )
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=1,
        first_bar_start_ms=0,
        last_bar_start_ms=0,
        file_size_bytes=len(content),
        file_sha256=file_sha,
    )
    return (
        ArtifactRecord(
            id=artifact_id,
            artifact_kind="metadata",
            market=spec.market,
            symbol=None,
            trading_date=None,
            resolution=None,
            data_type=None,
            provider="lean_image_extract",
            price_adjustment_mode=None,
            data_contract_hash=dch,
            file_path=file_path,
            file_sha256=file_sha,
            row_count=1,
            first_bar_start_ms=0,
            last_bar_start_ms=0,
        ),
        False,  # freshly fetched
    )


# ---------------------------------------------------------------------------
# Pass 1 helpers
# ---------------------------------------------------------------------------


async def _process_minute_trade_artifact(
    identity: ArtifactIdentity,
    spec: DataRunSpec,
) -> tuple[ArtifactRecord | None, ArtifactFailure | None, bool]:
    """Claim → fetch → write → complete one minute-trade artifact.

    Returns (record, None, was_reused) on success or (None, failure, False) on error.
    was_reused is True when the artifact already existed in the catalog (cache hit).
    """
    rel_path = LeanMinuteBarPath(
        market=identity.market,  # type: ignore[arg-type]
        symbol=identity.symbol or "",
        trading_date=identity.trading_date,  # type: ignore[arg-type]
        data_type="trade",
    ).relative_path()
    file_path = str(rel_path)
    dch = _minute_trade_dch()

    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        data_contract_hash=dch,
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
            return existing[0], None, True  # cache hit
        # In-flight elsewhere; Slice 1c doesn't poll. Report as transient.
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=identity.trading_date,
                data_type=identity.data_type,
                reason="lease_timeout",
                detail="another worker has the lease; polling not implemented in Slice 1c",
                attempt_count=1,
            ),
            False,
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
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=identity.trading_date,
                data_type=identity.data_type,
                reason="provider_auth_error",
                detail=str(e),
                attempt_count=1,
            ),
            False,
        )
    except PolygonEntitlementError as e:
        await catalog_client.fail_artifact(artifact_id, "provider_entitlement_error", str(e))
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=identity.trading_date,
                data_type=identity.data_type,
                reason="provider_entitlement_error",
                detail=str(e),
                attempt_count=1,
            ),
            False,
        )
    except PolygonRateLimitedError as e:
        await catalog_client.fail_artifact(artifact_id, "provider_rate_limited", str(e))
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=identity.trading_date,
                data_type=identity.data_type,
                reason="provider_rate_limited",
                detail=str(e),
                attempt_count=1,
            ),
            False,
        )
    except PolygonUnknownSymbolError as e:
        await catalog_client.fail_artifact(artifact_id, "unknown_symbol", str(e))
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=identity.trading_date,
                data_type=identity.data_type,
                reason="unknown_symbol",
                detail=str(e),
                attempt_count=1,
            ),
            False,
        )
    except PolygonFetchError as e:
        await catalog_client.fail_artifact(artifact_id, "provider_api_error", str(e))
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=identity.trading_date,
                data_type=identity.data_type,
                reason="provider_api_error",
                detail=str(e),
                attempt_count=1,
            ),
            False,
        )

    if not polygon_bars:
        await catalog_client.fail_artifact(artifact_id, "provider_no_data", "empty response")
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=identity.trading_date,
                data_type=identity.data_type,
                reason="provider_no_data",
                detail="Polygon returned no bars",
                attempt_count=1,
            ),
            False,
        )

    # Convert + encode + write.
    minute_bars = [_polygon_bar_to_minute_trade_bar(b) for b in polygon_bars]
    payload = build_minute_trade_zip_bytes(
        symbol=identity.symbol or "",
        trading_date_yyyymmdd=identity.trading_date.strftime("%Y%m%d"),  # type: ignore[union-attr]
        bars=minute_bars,
    )
    lake_root, staging_root = _lake_roots(spec)
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
            data_contract_hash=dch,
            file_path=file_path,
            file_sha256=file_sha,
            row_count=len(polygon_bars),
            first_bar_start_ms=first_bar_ms,
            last_bar_start_ms=last_bar_ms,
        ),
        None,
        False,  # freshly fetched
    )


async def _process_factor_file_artifact(
    identity: ArtifactIdentity,
    spec: DataRunSpec,
    minute_trade_records: list[ArtifactRecord],
    lake_root: Path,
) -> tuple[ArtifactRecord | None, ArtifactFailure | None, bool]:
    """Claim → fetch splits/dividends → build factor-file bytes → write → complete.

    ``minute_trade_records`` are the symbol's complete minute-trade
    artifacts from Pass 1; their RTH closes price the dividend rows
    (LEAN throws on a zero reference price — see ``factor_files``).

    Returns (record, None, is_reused) on success or (None, failure, False) on error.
    """
    rel_path = LeanFactorFilePath(
        market=identity.market,  # type: ignore[arg-type]
        symbol=identity.symbol or "",
    ).relative_path()
    file_path = str(rel_path)
    dch = _factor_file_dch(spec.start_trading_date, spec.end_trading_date)

    artifact_id = await catalog_client.claim_corp_action_artifact(
        identity=identity,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        data_contract_hash=dch,
        file_path=file_path,
    )
    if artifact_id is None:
        existing = await catalog_client.select_complete_corp_action_artifact(identity)
        if existing is not None:
            return existing, None, True  # cache hit
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=None,
                data_type=None,
                reason="lease_timeout",
                detail="factor_file in-flight elsewhere; polling not implemented in Slice 1c",
                attempt_count=1,
            ),
            False,
        )

    api_key = settings.POLYGON_API_KEY
    try:
        splits = await fetch_splits(symbol=identity.symbol or "", api_key=api_key)
        dividends = await fetch_dividends(symbol=identity.symbol or "", api_key=api_key)
    except Exception as e:
        await catalog_client.fail_artifact(artifact_id, "provider_api_error", str(e))
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=None,
                data_type=None,
                reason="provider_api_error",
                detail=str(e),
                attempt_count=1,
            ),
            False,
        )

    # Reference prices for the dividend rows come from the symbol's
    # captured minute bars (RTH closes only). A factor file with a
    # zero/missing reference price silently truncates LEAN backtests at
    # the first in-window dividend.
    all_bars: list[MinuteTradeBar] = []
    for src in sorted(minute_trade_records, key=lambda r: r.trading_date or spec.start_trading_date):
        try:
            all_bars.extend(_read_minute_trade_bars(src.file_path, lake_root))
        except Exception as e:
            await catalog_client.fail_artifact(artifact_id, "io_error", str(e))
            return (
                None,
                ArtifactFailure(
                    artifact_kind=identity.artifact_kind,
                    symbol=identity.symbol,
                    trading_date=None,
                    data_type=None,
                    reason="io_error",
                    detail=f"failed to read minute bars for factor-file reference prices: {e}",
                    attempt_count=1,
                ),
                False,
            )

    try:
        payload = build_factor_file_bytes(
            symbol=identity.symbol or "",
            splits=splits,
            dividends=dividends,
            history_start=spec.start_trading_date,
            history_end=spec.end_trading_date,
            daily_closes=rth_daily_closes(all_bars),
        )
    except FactorFileReferenceError as e:
        await catalog_client.fail_artifact(artifact_id, "internal_error", str(e))
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=None,
                data_type=None,
                reason="internal_error",
                detail=str(e),
                attempt_count=1,
            ),
            False,
        )
    _, staging_root = _lake_roots(spec)
    file_sha = atomic_write_and_promote(
        content=payload,
        lake_root=lake_root,
        staging_root=staging_root,
        rel_lake_path=rel_path,
        request_id=spec.request_id,
        worker_id=_WORKER_ID,
        attempt=1,
    )
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=len(splits) + len(dividends),
        first_bar_start_ms=0,
        last_bar_start_ms=0,
        file_size_bytes=len(payload),
        file_sha256=file_sha,
    )
    return (
        ArtifactRecord(
            id=artifact_id,
            artifact_kind=identity.artifact_kind,
            market=identity.market,
            symbol=identity.symbol,
            trading_date=None,
            resolution=None,
            data_type=None,
            provider=identity.provider,
            price_adjustment_mode=identity.price_adjustment_mode,
            data_contract_hash=dch,
            file_path=file_path,
            file_sha256=file_sha,
            row_count=len(splits) + len(dividends),
            first_bar_start_ms=0,
            last_bar_start_ms=0,
        ),
        None,
        False,  # freshly fetched
    )


async def _process_map_file_artifact(
    identity: ArtifactIdentity,
    spec: DataRunSpec,
) -> tuple[ArtifactRecord | None, ArtifactFailure | None, bool]:
    """Claim → fetch ticker events → build map-file bytes → write → complete.

    Returns (record, None, is_reused) on success or (None, failure, False) on error.
    """
    rel_path = LeanMapFilePath(
        market=identity.market,  # type: ignore[arg-type]
        symbol=identity.symbol or "",
    ).relative_path()
    file_path = str(rel_path)
    dch = _map_file_dch()

    artifact_id = await catalog_client.claim_corp_action_artifact(
        identity=identity,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        data_contract_hash=dch,
        file_path=file_path,
    )
    if artifact_id is None:
        existing = await catalog_client.select_complete_corp_action_artifact(identity)
        if existing is not None:
            return existing, None, True  # cache hit
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=None,
                data_type=None,
                reason="lease_timeout",
                detail="map_file in-flight elsewhere; polling not implemented in Slice 1c",
                attempt_count=1,
            ),
            False,
        )

    api_key = settings.POLYGON_API_KEY
    try:
        events = await fetch_ticker_events(symbol=identity.symbol or "", api_key=api_key)
    except Exception as e:
        await catalog_client.fail_artifact(artifact_id, "provider_api_error", str(e))
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=None,
                data_type=None,
                reason="provider_api_error",
                detail=str(e),
                attempt_count=1,
            ),
            False,
        )

    payload = build_map_file_bytes(
        symbol=identity.symbol or "",
        events=events,
        history_start=spec.start_trading_date,
        history_end=spec.end_trading_date,
        exchange="nyse",
    )
    lake_root, staging_root = _lake_roots(spec)
    file_sha = atomic_write_and_promote(
        content=payload,
        lake_root=lake_root,
        staging_root=staging_root,
        rel_lake_path=rel_path,
        request_id=spec.request_id,
        worker_id=_WORKER_ID,
        attempt=1,
    )
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=len(events),
        first_bar_start_ms=0,
        last_bar_start_ms=0,
        file_size_bytes=len(payload),
        file_sha256=file_sha,
    )
    return (
        ArtifactRecord(
            id=artifact_id,
            artifact_kind=identity.artifact_kind,
            market=identity.market,
            symbol=identity.symbol,
            trading_date=None,
            resolution=None,
            data_type=None,
            provider=identity.provider,
            price_adjustment_mode=identity.price_adjustment_mode,
            data_contract_hash=dch,
            file_path=file_path,
            file_sha256=file_sha,
            row_count=len(events),
            first_bar_start_ms=0,
            last_bar_start_ms=0,
        ),
        None,
        False,  # freshly fetched
    )


# ---------------------------------------------------------------------------
# Pass 2 helpers (derived artifacts)
# ---------------------------------------------------------------------------


async def _process_minute_quote_artifact(
    identity: ArtifactIdentity,
    source_trade_record: ArtifactRecord,
    spec: DataRunSpec,
    lake_root: Path,
) -> tuple[ArtifactRecord | None, ArtifactFailure | None, bool]:
    """Derive minute-quote bytes from same-day complete minute-trade artifact.

    Returns (record, None, is_reused) on success or (None, failure, False) on error.
    """
    rel_path = LeanMinuteBarPath(
        market=identity.market,  # type: ignore[arg-type]
        symbol=identity.symbol or "",
        trading_date=identity.trading_date,  # type: ignore[arg-type]
        data_type="quote",
    ).relative_path()
    file_path = str(rel_path)
    dch = _quote_dch(source_trade_record.id, source_trade_record.file_sha256)

    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        data_contract_hash=dch,
        file_path=file_path,
    )
    if artifact_id is None:
        existing = await catalog_client.select_coverage_minute_bars(
            market=identity.market,  # type: ignore[arg-type]
            symbol=identity.symbol,  # type: ignore[arg-type]
            data_type="quote",
            start_trading_date=identity.trading_date,  # type: ignore[arg-type]
            end_trading_date=identity.trading_date,  # type: ignore[arg-type]
        )
        if existing:
            return existing[0], None, True  # cache hit
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=identity.trading_date,
                data_type=identity.data_type,
                reason="lease_timeout",
                detail="minute-quote in-flight elsewhere; polling not implemented in Slice 1c",
                attempt_count=1,
            ),
            False,
        )

    # Read source trade bars from disk.
    try:
        trade_bars = _read_minute_trade_bars(source_trade_record.file_path, lake_root)
    except Exception as e:
        await catalog_client.fail_artifact(artifact_id, "io_error", str(e))
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=identity.trading_date,
                data_type=identity.data_type,
                reason="io_error",
                detail=f"failed to read source trade bars: {e}",
                attempt_count=1,
            ),
            False,
        )

    payload = build_minute_quote_zip_bytes(
        symbol=identity.symbol or "",
        trading_date_yyyymmdd=identity.trading_date.strftime("%Y%m%d"),  # type: ignore[union-attr]
        bars=trade_bars,
    )
    _, staging_root = _lake_roots(spec)
    file_sha = atomic_write_and_promote(
        content=payload,
        lake_root=lake_root,
        staging_root=staging_root,
        rel_lake_path=rel_path,
        request_id=spec.request_id,
        worker_id=_WORKER_ID,
        attempt=1,
    )
    row_count = len(trade_bars)
    first_ms = int(trade_bars[0].bar_start_et.timestamp() * 1000) if trade_bars else 0
    last_ms = int(trade_bars[-1].bar_start_et.timestamp() * 1000) if trade_bars else 0

    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=row_count,
        first_bar_start_ms=first_ms,
        last_bar_start_ms=last_ms,
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
            data_contract_hash=dch,
            file_path=file_path,
            file_sha256=file_sha,
            row_count=row_count,
            first_bar_start_ms=first_ms,
            last_bar_start_ms=last_ms,
        ),
        None,
        False,  # freshly derived
    )


async def _process_daily_trade_artifact(
    identity: ArtifactIdentity,
    source_trade_records: list[ArtifactRecord],
    spec: DataRunSpec,
    lake_root: Path,
) -> tuple[ArtifactRecord | None, ArtifactFailure | None, bool]:
    """Derive daily-trade bytes from all complete minute-trade artifacts for the symbol.

    Returns (record, None, is_reused) on success or (None, failure, False) on error.
    """
    rel_path = LeanDailyBarPath(
        market=identity.market,  # type: ignore[arg-type]
        symbol=identity.symbol or "",
    ).relative_path()
    file_path = str(rel_path)
    source_ids = [r.id for r in source_trade_records]
    source_shas = [r.file_sha256 for r in source_trade_records]
    dch = _daily_dch(source_ids, source_shas)

    artifact_id = await catalog_client.claim_aggregated_bar_artifact(
        identity=identity,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        data_contract_hash=dch,
        file_path=file_path,
    )
    if artifact_id is None:
        existing = await catalog_client.select_complete_aggregated_bar_artifact(identity)
        if existing is not None:
            if existing.data_contract_hash == dch:
                return existing, None, True  # cache hit — same source set
            # The existing daily artifact was built from a different set of source
            # minute artifacts (different window or different corp-action history).
            # Return a failure so Backend / the caller can decide whether to
            # force_refresh. Re-aggregation is out of scope for Slice 1c.
            return (
                None,
                ArtifactFailure(
                    artifact_kind=identity.artifact_kind,
                    symbol=identity.symbol,
                    trading_date=None,
                    data_type=identity.data_type,
                    reason="data_contract_mismatch",
                    detail=(
                        f"cached daily artifact hash {existing.data_contract_hash!r} "
                        f"differs from newly computed {dch!r}; "
                        "re-run with force_refresh=True to rebuild"
                    ),
                    attempt_count=1,
                ),
                False,
            )
        return (
            None,
            ArtifactFailure(
                artifact_kind=identity.artifact_kind,
                symbol=identity.symbol,
                trading_date=None,
                data_type=identity.data_type,
                reason="lease_timeout",
                detail="daily-trade in-flight elsewhere; polling not implemented in Slice 1c",
                attempt_count=1,
            ),
            False,
        )

    # Read all source trade bars from disk.
    all_bars: list[MinuteTradeBar] = []
    for src in sorted(source_trade_records, key=lambda r: r.trading_date or spec.start_trading_date):
        try:
            bars = _read_minute_trade_bars(src.file_path, lake_root)
            all_bars.extend(bars)
        except Exception as e:
            await catalog_client.fail_artifact(artifact_id, "io_error", str(e))
            return (
                None,
                ArtifactFailure(
                    artifact_kind=identity.artifact_kind,
                    symbol=identity.symbol,
                    trading_date=None,
                    data_type=identity.data_type,
                    reason="io_error",
                    detail=f"failed to read source trade bars from {src.file_path}: {e}",
                    attempt_count=1,
                ),
                False,
            )

    aggregates = aggregate_minute_to_daily(all_bars)
    payload = build_daily_zip_bytes(symbol=identity.symbol or "", aggregates=aggregates)
    _, staging_root = _lake_roots(spec)
    file_sha = atomic_write_and_promote(
        content=payload,
        lake_root=lake_root,
        staging_root=staging_root,
        rel_lake_path=rel_path,
        request_id=spec.request_id,
        worker_id=_WORKER_ID,
        attempt=1,
    )
    row_count = len(aggregates)
    await catalog_client.complete_artifact(
        artifact_id=artifact_id,
        row_count=row_count,
        first_bar_start_ms=0,
        last_bar_start_ms=0,
        file_size_bytes=len(payload),
        file_sha256=file_sha,
    )
    return (
        ArtifactRecord(
            id=artifact_id,
            artifact_kind=identity.artifact_kind,
            market=identity.market,
            symbol=identity.symbol,
            trading_date=None,
            resolution=identity.resolution,
            data_type=identity.data_type,
            provider=identity.provider,
            price_adjustment_mode=identity.price_adjustment_mode,
            data_contract_hash=dch,
            file_path=file_path,
            file_sha256=file_sha,
            row_count=row_count,
            first_bar_start_ms=0,
            last_bar_start_ms=0,
        ),
        None,
        False,  # freshly derived
    )


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


async def ensure_data(spec: DataRunSpec) -> DataAvailabilityResult:
    """Full Slice 1c pipeline: Phase 0 metadata bootstrap + Pass 1 + Pass 2.

    Phase 0: Extract LEAN metadata (market-hours + symbol-properties) from the
    launcher. The market-hours path is passed into expand_required_artifacts so
    trading_sessions_for can use the real calendar instead of the hardcoded
    holiday list.

    Pass 1: Polygon-sourced artifacts — minute-trade, factor_file, map_file.

    Pass 2: Derived artifacts — minute-quote (from same-day trade artifact),
    daily-trade (from all same-symbol trade artifacts). Runs after Pass 1.
    """
    started_ms = int(time.time() * 1000)
    lake_root, staging_root = _lake_roots(spec)

    # Ensure pool exists. init_pool is idempotent; pool stays alive across calls.
    await catalog_client.init_pool()

    artifacts: list[ArtifactRecord] = []
    failures: list[ArtifactFailure] = []
    fetched_count = 0
    reused_count = 0

    # -----------------------------------------------------------------------
    # Phase 0: LEAN metadata bootstrap
    # -----------------------------------------------------------------------
    mh_rel = LeanMetadataPath(kind="market_hours").relative_path()
    sp_rel = LeanMetadataPath(kind="symbol_properties").relative_path()

    mh_record, mh_reused = await _bootstrap_metadata_artifact(
        file_name="market-hours-database.json",
        metadata_kind="market_hours",
        rel_path=mh_rel,
        lean_image_digest=spec.lean_image_digest,
        spec=spec,
        lake_root=lake_root,
        staging_root=staging_root,
    )
    sp_record, sp_reused = await _bootstrap_metadata_artifact(
        file_name="symbol-properties-database.csv",
        metadata_kind="symbol_properties",
        rel_path=sp_rel,
        lean_image_digest=spec.lean_image_digest,
        spec=spec,
        lake_root=lake_root,
        staging_root=staging_root,
    )

    # If market-hours was successfully written (or already existed), pass the
    # on-disk path to expand_required_artifacts so sessions use the real calendar.
    mh_db_path: Path | None = None
    if mh_record is not None:
        artifacts.append(mh_record)
        if mh_reused:
            reused_count += 1
        else:
            fetched_count += 1
        mh_db_path = lake_root / Path(*mh_rel.parts)
    else:
        # Bootstrap failure — surface as ArtifactFailure so Backend's
        # partial-coverage policy can gate. Sessions will fall back to the
        # hardcoded calendar; the caller can decide whether that is acceptable.
        failures.append(
            ArtifactFailure(
                artifact_kind="metadata",
                symbol=None,
                trading_date=None,
                data_type=None,
                reason="io_error",
                detail="market-hours metadata bootstrap failed; see launcher logs",
                attempt_count=1,
            )
        )

    if sp_record is not None:
        artifacts.append(sp_record)
        if sp_reused:
            reused_count += 1
        else:
            fetched_count += 1
    else:
        failures.append(
            ArtifactFailure(
                artifact_kind="metadata",
                symbol=None,
                trading_date=None,
                data_type=None,
                reason="io_error",
                detail="symbol-properties metadata bootstrap failed; see launcher logs",
                attempt_count=1,
            )
        )

    # -----------------------------------------------------------------------
    # Expand required artifacts (now with real calendar if available)
    # -----------------------------------------------------------------------
    required, non_sessions = expand_required_artifacts(spec, market_hours_db_path=mh_db_path)

    # -----------------------------------------------------------------------
    # Pass 1: Polygon-sourced artifacts (minute-trade + factor_file + map_file)
    # -----------------------------------------------------------------------
    # minute-trade records keyed by (symbol, trading_date) for Pass 2 use.
    minute_trade_by_symbol: dict[str, list[ArtifactRecord]] = {}
    minute_trade_by_date: dict[tuple[str, str], ArtifactRecord] = {}

    for identity in required:
        if _is_minute_trade(identity):
            record, failure, is_reused = await _process_minute_trade_artifact(identity, spec)
            if record is not None:
                artifacts.append(record)
                if is_reused:
                    reused_count += 1
                else:
                    fetched_count += 1
                sym = identity.symbol or ""
                minute_trade_by_symbol.setdefault(sym, []).append(record)
                date_str = identity.trading_date.isoformat() if identity.trading_date else ""
                minute_trade_by_date[(sym, date_str)] = record
            elif failure is not None:
                failures.append(failure)

        elif identity.artifact_kind == "factor_file":
            # expand_required_artifacts emits a symbol's minute days before
            # its factor_file, so minute_trade_by_symbol is fully populated
            # here and supplies the dividend rows' reference prices.
            #
            # Gate on FULL per-symbol minute coverage. With a gap, the
            # dividend's prior-session reference price would silently bind
            # to an older available close (factor_files._trading_day_before
            # only raises when there is NO prior session at all) and drift
            # parity. Fail the factor file instead.
            sym = identity.symbol or ""
            expected_minute_days = sum(1 for req in required if _is_minute_trade(req) and (req.symbol or "") == sym)
            available_minute_days = len(minute_trade_by_symbol.get(sym, []))
            if available_minute_days != expected_minute_days:
                failures.append(
                    ArtifactFailure(
                        artifact_kind=identity.artifact_kind,
                        symbol=identity.symbol,
                        trading_date=None,
                        data_type=None,
                        reason="internal_error",
                        detail=(
                            f"incomplete minute-trade coverage for factor-file build: "
                            f"{available_minute_days}/{expected_minute_days} sessions for {sym}; "
                            "reference prices would drift — fix the minute-bar failures and rerun"
                        ),
                        attempt_count=1,
                    )
                )
                continue
            record, failure, is_reused = await _process_factor_file_artifact(
                identity,
                spec,
                minute_trade_by_symbol.get(sym, []),
                lake_root,
            )
            if record is not None:
                artifacts.append(record)
                if is_reused:
                    reused_count += 1
                else:
                    fetched_count += 1
            elif failure is not None:
                failures.append(failure)

        elif identity.artifact_kind == "map_file":
            record, failure, is_reused = await _process_map_file_artifact(identity, spec)
            if record is not None:
                artifacts.append(record)
                if is_reused:
                    reused_count += 1
                else:
                    fetched_count += 1
            elif failure is not None:
                failures.append(failure)

    # -----------------------------------------------------------------------
    # Pass 2: Derived artifacts (minute-quote + daily-trade)
    # -----------------------------------------------------------------------
    for identity in required:
        if _is_minute_quote(identity):
            sym = identity.symbol or ""
            date_str = identity.trading_date.isoformat() if identity.trading_date else ""
            source = minute_trade_by_date.get((sym, date_str))
            if source is None:
                # No source trade artifact available (it failed in Pass 1).
                failures.append(
                    ArtifactFailure(
                        artifact_kind=identity.artifact_kind,
                        symbol=identity.symbol,
                        trading_date=identity.trading_date,
                        data_type=identity.data_type,
                        reason="internal_error",
                        detail=f"no complete minute-trade source for ({sym}, {date_str})",
                        attempt_count=1,
                    )
                )
                continue
            record, failure, is_reused = await _process_minute_quote_artifact(identity, source, spec, lake_root)
            if record is not None:
                artifacts.append(record)
                if is_reused:
                    reused_count += 1
                else:
                    fetched_count += 1
            elif failure is not None:
                failures.append(failure)

        elif _is_daily_trade(identity):
            sym = identity.symbol or ""
            source_records = minute_trade_by_symbol.get(sym, [])
            if not source_records:
                failures.append(
                    ArtifactFailure(
                        artifact_kind=identity.artifact_kind,
                        symbol=identity.symbol,
                        trading_date=None,
                        data_type=identity.data_type,
                        reason="internal_error",
                        detail=f"no complete minute-trade sources for symbol {sym}",
                        attempt_count=1,
                    )
                )
                continue
            record, failure, is_reused = await _process_daily_trade_artifact(identity, source_records, spec, lake_root)
            if record is not None:
                artifacts.append(record)
                if is_reused:
                    reused_count += 1
                else:
                    fetched_count += 1
            elif failure is not None:
                failures.append(failure)

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
        lean_data_root_path=str(lake_root),
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
