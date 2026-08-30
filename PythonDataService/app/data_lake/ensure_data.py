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
from typing import Literal
from zoneinfo import ZoneInfo

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.atomic import ArtifactLeaseLostError, publish_artifact
from app.data_lake.data_contract import data_contract_hash as _dch
from app.data_lake.derived_daily import (
    aggregate_minute_to_daily,
    build_daily_zip_bytes,
    rth_daily_closes,
)
from app.data_lake.derived_quote import build_minute_quote_zip_bytes
from app.data_lake.factor_files import FactorFileReferenceError, build_factor_file_bytes
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.map_files import build_map_file_bytes
from app.data_lake.metadata_bundle import ensure_lean_metadata_bundle
from app.data_lake.path_policy import (
    LeanDailyBarPath,
    LeanFactorFilePath,
    LeanMapFilePath,
    LeanMinuteBarPath,
    ensure_lean_readable_layout,
    resolve_lake_root,
    resolve_staging_root,
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
    PriceAdjustmentMode,
    classify_overall_status,
)

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_WORKER_ID = os.environ.get("HOSTNAME", "py-data-lake")  # one writer per process
_LEASE_TTL_MS = 300_000
# A row that has failed this many times is reported as a terminal failure
# rather than reclaimed again. Matches the retry budget already exercised by
# tests/unit/data_lake/test_catalog_write_ops.py's steal_or_retry coverage.
_MAX_CLAIM_RETRIES = 3

# data_contract_hash provider params (canonical per artifact kind).
# ``adjusted`` is filled per request from the run's mode -- it is the literal
# vendor query parameter, so pinning it here would have made every adjusted
# artifact claim the raw recipe's contract hash.
_DCH_MINUTE_TRADE_PARAMS = {
    "timespan": "minute",
    "multiplier": 1,
    "endpoint": "v2/aggs",
}


def _polygon_adjusted_flag(price_adjustment_mode: PriceAdjustmentMode) -> bool:
    """The vendor's ``adjusted`` query parameter for one lake mode.

    ``lean_adjusted`` is a reserved enum value with no producer: it would be
    derived from raw bars plus factor files, not fetched, so there is no
    vendor flag that means it. Refuse rather than silently fetching one of
    the other two under its name.
    """
    if price_adjustment_mode == "raw":
        return False
    if price_adjustment_mode == "polygon_split_adjusted":
        return True
    raise ValueError(
        f"{price_adjustment_mode!r} is not a fetchable adjustment mode; it would have to be "
        "derived from raw bars and factor files, and nothing derives it yet"
    )
_DCH_FACTOR_FILE_PARAMS = {
    "endpoints": ["v3/reference/splits", "v3/reference/dividends"],
}
_DCH_MAP_FILE_PARAMS = {
    "endpoint": "v3/reference/tickers/{sym}/events",
}


def _writable_lake_roots(spec: DataRunSpec) -> tuple[Path, Path]:
    """Resolve this run's lake and staging roots, creating both.

    The writer creates its own roots. ``resolve_lake_root`` deliberately does
    not — for a *reader*, a missing root is a legitimate "the lake holds
    nothing yet for this mode" — but ``atomic.assert_same_filesystem`` needs
    both to exist before it can compare their devices, so somebody has to,
    and it should be the side that is about to write.

    This became load-bearing with #1839: the mode is now a path segment, so
    the first write in a new adjustment mode faces a directory that has never
    existed, not merely an empty one.
    """
    lake_root = resolve_lake_root(spec.price_adjustment_mode)
    staging_root = resolve_staging_root(spec.price_adjustment_mode)
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    return lake_root, staging_root


async def _publish_under_lease(
    *,
    identity: ArtifactIdentity,
    payload: bytes,
    lake_root: Path,
    staging_root: Path,
    rel_path: PurePosixPath,
    spec: DataRunSpec,
    artifact_id: int,
    lease_generation: int,
    trading_date: date | None,
    row_count: int,
    first_bar_start_ms: int,
    last_bar_start_ms: int,
    data_contract_hash: str | None = None,
) -> tuple[str, None] | tuple[None, ArtifactFailure]:
    """Publish one claimed artifact -- bytes onto the lake and receipt into
    the catalog, as one operation (issue #1888).

    Every Pass-1/Pass-2 ``_process_*_artifact`` function claims (or reclaims)
    a row, fetches or derives bytes, then reaches this one call. Promotion
    and completion are deliberately not separable here: they happen inside a
    single catalog transaction holding the artifact's row lock, so a writer
    that lost its lease can neither overwrite the winner's file nor record a
    receipt describing bytes it never managed to publish. Centralized so the
    "lease lost mid-write" outcome is classified exactly once.

    Returns ``(file_sha, None)`` on success or ``(None, failure)`` when the
    catalog refused to authorize this writer -- the caller folds ``failure``
    into its own return shape (each ``_process_*`` function's third tuple
    element has a different type, so that fold can't live here too). On a
    refusal the caller must not call ``fail_artifact``: the row belongs to
    another generation now, and failing it would clobber the winner.
    """
    try:
        file_sha = await publish_artifact(
            content=payload,
            lake_root=lake_root,
            staging_root=staging_root,
            rel_lake_path=rel_path,
            request_id=spec.request_id,
            worker_id=_WORKER_ID,
            attempt=1,
            artifact_id=artifact_id,
            lease_generation=lease_generation,
            row_count=row_count,
            first_bar_start_ms=first_bar_start_ms,
            last_bar_start_ms=last_bar_start_ms,
            data_contract_hash=data_contract_hash,
        )
    except ArtifactLeaseLostError as e:
        return None, ArtifactFailure(
            artifact_kind=identity.artifact_kind,
            symbol=identity.symbol,
            trading_date=trading_date,
            data_type=identity.data_type,
            reason="lease_timeout",
            detail=str(e),
            attempt_count=1,
        )
    return file_sha, None


def _minute_trade_dch(price_adjustment_mode: PriceAdjustmentMode) -> str:
    return _dch(
        provider="polygon",
        provider_params={
            **_DCH_MINUTE_TRADE_PARAMS,
            "adjusted": _polygon_adjusted_flag(price_adjustment_mode),
        },
        price_adjustment_mode=price_adjustment_mode,
        session_policy="full",
        lean_format_version=1,
    )


def _factor_file_dch(
    history_start: date, history_end: date, price_adjustment_mode: PriceAdjustmentMode
) -> str:
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
        price_adjustment_mode=price_adjustment_mode,
        session_policy="full",
        lean_format_version=1,
    )


def _map_file_dch(price_adjustment_mode: PriceAdjustmentMode) -> str:
    return _dch(
        provider="polygon",
        provider_params=_DCH_MAP_FILE_PARAMS,
        price_adjustment_mode=price_adjustment_mode,
        session_policy="full",
        lean_format_version=1,
    )


def _quote_dch(
    source_artifact_id: int, source_file_sha256: str, price_adjustment_mode: PriceAdjustmentMode
) -> str:
    return _dch(
        provider="learn_ai_derived",
        provider_params={
            "source": "minute-trade",
            "source_artifact_id": source_artifact_id,
            "source_file_sha256": source_file_sha256,
        },
        price_adjustment_mode=price_adjustment_mode,
        session_policy="full",
        lean_format_version=1,
    )


def _daily_dch(
    source_artifact_ids: list[int],
    source_file_sha256s: list[str],
    price_adjustment_mode: PriceAdjustmentMode,
) -> str:
    return _dch(
        provider="learn_ai_derived",
        provider_params={
            "source": "minute-trade",
            "source_artifact_ids": sorted(source_artifact_ids),
            "source_file_sha256s": sorted(source_file_sha256s),
        },
        price_adjustment_mode=price_adjustment_mode,
        session_policy="full",
        lean_format_version=1,
    )


def provider_for_data_type(data_type: Literal["trade", "quote"]) -> str:
    """Return the catalog Provider identity for a minute-bar data_type.

    Trade minute-bars come straight from Polygon. Quote minute-bars are
    synthesized in-process from same-day trade bytes (DataRunSpec requires
    'trade' whenever 'quote' is requested) and are catalogued under
    'learn_ai_derived' rather than 'polygon' — this is the single source
    for that mapping; minute_bar_identity below and the coverage
    endpoint (app/routers/data_lake.py) both call it so they cannot drift.
    """
    return "polygon" if data_type == "trade" else "learn_ai_derived"


def minute_bar_identity(
    spec: DataRunSpec,
    *,
    symbol: str | None,
    trading_date: date | None,
    data_type: str | None,
) -> ArtifactIdentity:
    """Canonical minute-bar ArtifactIdentity builder.

    Single source of truth for the (provider, price_adjustment_mode) pair
    a minute-bar identity carries: the provider comes from
    provider_for_data_type (the one mapping the coverage endpoint also
    uses), at the spec's own price-adjustment mode (never a hardcoded
    literal that could drift from it). Both expand_required_artifacts's
    inner loop (below) and the backfill job's lease-wait poll
    (app.data_lake.backfill._wait_for_lease_resolution) call this so they
    can't independently drift on the provider ternary.
    """
    return ArtifactIdentity(
        artifact_kind="time_series_bars",
        market=spec.market,
        symbol=symbol,
        trading_date=trading_date,
        resolution="minute",
        data_type=data_type,
        provider=provider_for_data_type(data_type),
        price_adjustment_mode=spec.price_adjustment_mode,
    )


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
    sessions, non_sessions = trading_sessions_for(
        spec.market,
        spec.start_trading_date,
        spec.end_trading_date,
    )
    required: list[ArtifactIdentity] = []

    for symbol in sorted(spec.symbols):
        # Per-day minute bars.
        for trading_date in sessions:
            for data_type in spec.data_types:
                required.append(minute_bar_identity(spec, symbol=symbol, trading_date=trading_date, data_type=data_type))

        # Corp-action artifacts.
        if spec.include_factor_files:
            required.append(
                ArtifactIdentity(
                    artifact_kind="factor_file",
                    market=spec.market,
                    symbol=symbol,
                    provider="polygon",
                    price_adjustment_mode=spec.price_adjustment_mode,
                )
            )
        if spec.include_map_files:
            required.append(
                ArtifactIdentity(
                    artifact_kind="map_file",
                    market=spec.market,
                    symbol=symbol,
                    provider="polygon",
                    price_adjustment_mode=spec.price_adjustment_mode,
                )
            )

        # Daily-trade derived artifact (per symbol, null trading_date).
        if "trade" in spec.data_types and spec.include_daily_trade:
            required.append(
                ArtifactIdentity(
                    artifact_kind="time_series_bars",
                    market=spec.market,
                    symbol=symbol,
                    trading_date=None,
                    resolution="daily",
                    data_type="trade",
                    provider="learn_ai_derived",
                    price_adjustment_mode=spec.price_adjustment_mode,
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
    dch = _minute_trade_dch(spec.price_adjustment_mode)

    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        data_contract_hash=dch,
        file_path=file_path,
    )
    lease_generation = catalog_client.INITIAL_LEASE_GENERATION
    if artifact_id is None:
        # Already complete (or in-flight); read the existing complete row.
        # price_adjustment_mode is passed explicitly (not left to the
        # query's "match any mode" default): app.data_lake.cache_import can
        # now put a 'polygon_split_adjusted' row in the catalog for the same
        # (market, symbol, date, data_type) this 'raw' identity claims, and
        # picking the wrong one here would silently launder into a
        # nondeterministic downstream quote data_contract_hash (_quote_dch
        # below keys off this record's id/file_sha256).
        existing = await catalog_client.select_coverage_minute_bars(
            market=identity.market,  # type: ignore[arg-type]
            symbol=identity.symbol,  # type: ignore[arg-type]
            data_type="trade",
            start_trading_date=identity.trading_date,  # type: ignore[arg-type]
            end_trading_date=identity.trading_date,  # type: ignore[arg-type]
            price_adjustment_mode=identity.price_adjustment_mode,
        )
        if existing:
            return existing[0], None, True  # cache hit

        # Not complete either — the row exists but is 'failed' or 'fetching'.
        # Those need different answers: a 'failed' (or lease-expired
        # 'fetching') row is not contention, it is a done deal, and reporting
        # it as lease_timeout would send the caller into a 600s poll loop that
        # can never resolve, because the row never transitions on its own.
        # Reclaim it here instead — the same primitive the lease-expiry sweep
        # uses — so this call either gets a fresh attempt at the bytes or a
        # terminal answer, on this pass.
        row_state = await catalog_client.select_minute_bar_claim_state(identity)
        if row_state is not None:
            reclaimed_generation = await catalog_client.steal_or_retry_minute_bar(
                artifact_id=row_state.id,
                worker_id=_WORKER_ID,
                lease_ttl_ms=_LEASE_TTL_MS,
                max_retries=_MAX_CLAIM_RETRIES,
            )
            if reclaimed_generation is not None:
                artifact_id = row_state.id
                lease_generation = reclaimed_generation
                # Falls through to the fetch below, exactly as a fresh claim would.
            elif row_state.status == "failed":
                # Retries exhausted: a real, terminal failure, not contention.
                # fetch_timeout (not lease_timeout) so the bridge's contention
                # classifier does not send this back into the poll loop.
                return (
                    None,
                    ArtifactFailure(
                        artifact_kind=identity.artifact_kind,
                        symbol=identity.symbol,
                        trading_date=identity.trading_date,
                        data_type=identity.data_type,
                        reason="fetch_timeout",
                        detail=(
                            f"exhausted {row_state.attempt_count} attempt(s); "
                            f"last error: {row_state.last_error}"
                        ),
                        attempt_count=row_state.attempt_count,
                    ),
                    False,
                )
        if artifact_id is None:
            # Genuinely fetching under a live lease elsewhere — real contention.
            return (
                None,
                ArtifactFailure(
                    artifact_kind=identity.artifact_kind,
                    symbol=identity.symbol,
                    trading_date=identity.trading_date,
                    data_type=identity.data_type,
                    reason="lease_timeout",
                    detail="another worker has the lease",
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
            adjusted=_polygon_adjusted_flag(spec.price_adjustment_mode),
        )
    except PolygonAuthError as e:
        await catalog_client.fail_artifact(artifact_id, "provider_auth_error", str(e), worker_id=_WORKER_ID, lease_generation=lease_generation)
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
        await catalog_client.fail_artifact(artifact_id, "provider_entitlement_error", str(e), worker_id=_WORKER_ID, lease_generation=lease_generation)
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
        await catalog_client.fail_artifact(artifact_id, "provider_rate_limited", str(e), worker_id=_WORKER_ID, lease_generation=lease_generation)
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
        await catalog_client.fail_artifact(artifact_id, "unknown_symbol", str(e), worker_id=_WORKER_ID, lease_generation=lease_generation)
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
        await catalog_client.fail_artifact(artifact_id, "provider_api_error", str(e), worker_id=_WORKER_ID, lease_generation=lease_generation)
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
        await catalog_client.fail_artifact(artifact_id, "provider_no_data", "empty response", worker_id=_WORKER_ID, lease_generation=lease_generation)
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
    lake_root, staging_root = _writable_lake_roots(spec)
    first_bar_ms = polygon_bars[0].t_ms
    last_bar_ms = polygon_bars[-1].t_ms
    file_sha, lease_failure = await _publish_under_lease(
        identity=identity,
        payload=payload,
        lake_root=lake_root,
        staging_root=staging_root,
        rel_path=rel_path,
        spec=spec,
        artifact_id=artifact_id,
        lease_generation=lease_generation,
        trading_date=identity.trading_date,
        row_count=len(polygon_bars),
        first_bar_start_ms=first_bar_ms,
        last_bar_start_ms=last_bar_ms,
    )
    if lease_failure is not None:
        return None, lease_failure, False

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
            file_size_bytes=len(payload),
            data_root_id=identity.data_root_id,
        ),
        None,
        False,  # freshly fetched
    )


async def _process_factor_file_artifact(
    identity: ArtifactIdentity,
    spec: DataRunSpec,
    minute_trade_records: list[ArtifactRecord],
    lake_root: Path,
) -> tuple[ArtifactRecord | None, ArtifactFailure | None, Literal["fetched", "reused", "refreshed"]]:
    """Claim → fetch splits/dividends → build factor-file bytes → write → complete.

    ``minute_trade_records`` are the symbol's complete minute-trade
    artifacts from Pass 1; their RTH closes price the dividend rows
    (LEAN throws on a zero reference price — see ``factor_files``).

    A factor file's DataContractHash is derived from the request's history
    window (``_factor_file_dch``), unlike map_file's (window-independent —
    see ``_map_file_dch``). A wider window therefore produces a different
    hash for the same symbol, and this rebuilds onto it — same
    refresh-on-mismatch model as ``_process_daily_trade_artifact`` — instead
    of silently serving back a factor file anchored to the earlier, narrower
    window's history bounds.

    Returns (record, None, outcome) on success or (None, failure, "fetched")
    on error — the third element is meaningless on failure.
    """
    rel_path = LeanFactorFilePath(
        market=identity.market,  # type: ignore[arg-type]
        symbol=identity.symbol or "",
    ).relative_path()
    file_path = str(rel_path)
    dch = _factor_file_dch(spec.start_trading_date, spec.end_trading_date, spec.price_adjustment_mode)

    outcome: Literal["fetched", "reused", "refreshed"] = "fetched"
    artifact_id = await catalog_client.claim_corp_action_artifact(
        identity=identity,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        data_contract_hash=dch,
        file_path=file_path,
    )
    lease_generation = catalog_client.INITIAL_LEASE_GENERATION
    if artifact_id is None:
        existing = await catalog_client.select_complete_corp_action_artifact(identity)
        if existing is not None:
            if existing.data_contract_hash == dch:
                return existing, None, "reused"  # cache hit — same history window
            prior = await catalog_client.refresh_complete_artifact(
                artifact_id=existing.id,
                worker_id=_WORKER_ID,
                lease_ttl_ms=_LEASE_TTL_MS,
            )
            if prior is None:
                # Raced with another worker's own refresh/claim between the two
                # selects above; the caller's next ensure_data call retries.
                return (
                    None,
                    ArtifactFailure(
                        artifact_kind=identity.artifact_kind,
                        symbol=identity.symbol,
                        trading_date=None,
                        data_type=None,
                        reason="lease_timeout",
                        detail="factor_file rebuild raced with another worker; retry on a later ensure_data call",
                        attempt_count=1,
                    ),
                    "fetched",
                )
            artifact_id = existing.id
            lease_generation = prior.new_lease_generation
            outcome = "refreshed"
        else:
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
                "fetched",
            )

    # A rebuild (outcome == "refreshed") that fails anywhere below hasn't
    # written anything new yet — restore the previously-complete artifact
    # rather than marking it 'failed' with no retry path (steal_or_retry_
    # minute_bar doesn't cover corp-action rows). A first-ever fetch
    # (outcome == "fetched") has no prior state to restore, so fail as before.
    async def _fail_or_restore(last_error: str, detail: str) -> None:
        if outcome == "refreshed":
            await catalog_client.restore_complete_artifact(artifact_id, _WORKER_ID, lease_generation)
        else:
            await catalog_client.fail_artifact(artifact_id, last_error, detail, worker_id=_WORKER_ID, lease_generation=lease_generation)

    api_key = settings.POLYGON_API_KEY
    try:
        splits = await fetch_splits(symbol=identity.symbol or "", api_key=api_key)
        dividends = await fetch_dividends(symbol=identity.symbol or "", api_key=api_key)
    except Exception as e:
        await _fail_or_restore("provider_api_error", str(e))
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
            "fetched",
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
            await _fail_or_restore("io_error", str(e))
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
                "fetched",
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
        await _fail_or_restore("internal_error", str(e))
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
            "fetched",
        )
    staging_root = resolve_staging_root(spec.price_adjustment_mode)
    file_sha, lease_failure = await _publish_under_lease(
        identity=identity,
        payload=payload,
        lake_root=lake_root,
        staging_root=staging_root,
        rel_path=rel_path,
        spec=spec,
        artifact_id=artifact_id,
        lease_generation=lease_generation,
        trading_date=None,
        row_count=len(splits) + len(dividends),
        first_bar_start_ms=0,
        last_bar_start_ms=0,
        # Rebuild path (outcome == "refreshed") completes onto a different
        # history window's hash than the row's existing DataContractHash —
        # see the identical comment in _process_daily_trade_artifact.
        data_contract_hash=dch,
    )
    if lease_failure is not None:
        return None, lease_failure, "fetched"
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
            file_size_bytes=len(payload),
            data_root_id=identity.data_root_id,
        ),
        None,
        outcome,
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
    dch = _map_file_dch(spec.price_adjustment_mode)

    artifact_id = await catalog_client.claim_corp_action_artifact(
        identity=identity,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        data_contract_hash=dch,
        file_path=file_path,
    )
    lease_generation = catalog_client.INITIAL_LEASE_GENERATION
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
        await catalog_client.fail_artifact(artifact_id, "provider_api_error", str(e), worker_id=_WORKER_ID, lease_generation=lease_generation)
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
    lake_root, staging_root = _writable_lake_roots(spec)
    file_sha, lease_failure = await _publish_under_lease(
        identity=identity,
        payload=payload,
        lake_root=lake_root,
        staging_root=staging_root,
        rel_path=rel_path,
        spec=spec,
        artifact_id=artifact_id,
        lease_generation=lease_generation,
        trading_date=None,
        row_count=len(events),
        first_bar_start_ms=0,
        last_bar_start_ms=0,
    )
    if lease_failure is not None:
        return None, lease_failure, False
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
            file_size_bytes=len(payload),
            data_root_id=identity.data_root_id,
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
    dch = _quote_dch(source_trade_record.id, source_trade_record.file_sha256, spec.price_adjustment_mode)

    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        data_contract_hash=dch,
        file_path=file_path,
    )
    lease_generation = catalog_client.INITIAL_LEASE_GENERATION
    if artifact_id is None:
        # price_adjustment_mode scoped explicitly for the same reason as the
        # minute-trade lookup above: a coexisting different-mode row for
        # this (market, symbol, date, data_type) must never be picked here.
        existing = await catalog_client.select_coverage_minute_bars(
            market=identity.market,  # type: ignore[arg-type]
            symbol=identity.symbol,  # type: ignore[arg-type]
            data_type="quote",
            start_trading_date=identity.trading_date,  # type: ignore[arg-type]
            end_trading_date=identity.trading_date,  # type: ignore[arg-type]
            price_adjustment_mode=identity.price_adjustment_mode,
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
        await catalog_client.fail_artifact(artifact_id, "io_error", str(e), worker_id=_WORKER_ID, lease_generation=lease_generation)
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
    staging_root = resolve_staging_root(spec.price_adjustment_mode)
    row_count = len(trade_bars)
    first_ms = int(trade_bars[0].bar_start_et.timestamp() * 1000) if trade_bars else 0
    last_ms = int(trade_bars[-1].bar_start_et.timestamp() * 1000) if trade_bars else 0
    file_sha, lease_failure = await _publish_under_lease(
        identity=identity,
        payload=payload,
        lake_root=lake_root,
        staging_root=staging_root,
        rel_path=rel_path,
        spec=spec,
        artifact_id=artifact_id,
        lease_generation=lease_generation,
        trading_date=identity.trading_date,
        row_count=row_count,
        first_bar_start_ms=first_ms,
        last_bar_start_ms=last_ms,
    )
    if lease_failure is not None:
        return None, lease_failure, False
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
            file_size_bytes=len(payload),
            data_root_id=identity.data_root_id,
        ),
        None,
        False,  # freshly derived
    )


async def _process_daily_trade_artifact(
    identity: ArtifactIdentity,
    source_trade_records: list[ArtifactRecord],
    spec: DataRunSpec,
    lake_root: Path,
) -> tuple[ArtifactRecord | None, ArtifactFailure | None, Literal["fetched", "reused", "refreshed"]]:
    """Derive daily-trade bytes from all complete minute-trade artifacts for the symbol.

    ``source_trade_records`` is the symbol's full current minute-trade
    coverage (the caller queries the catalog directly, not just this call's
    requested window — see the Pass 2 call site), so the resulting
    DataContractHash reflects everything currently catalogued, not one
    window. That is what makes the artifact rebuildable across successive
    windows instead of colliding: a second, wider (or narrower, or
    corrected) window naturally produces a different hash over a different
    source set, and this function rebuilds onto it rather than refusing.

    Returns (record, None, outcome) on success or (None, failure, "fetched")
    on error — the third element is meaningless on failure.
    """
    rel_path = LeanDailyBarPath(
        market=identity.market,  # type: ignore[arg-type]
        symbol=identity.symbol or "",
    ).relative_path()
    file_path = str(rel_path)
    source_ids = [r.id for r in source_trade_records]
    source_shas = [r.file_sha256 for r in source_trade_records]
    dch = _daily_dch(source_ids, source_shas, spec.price_adjustment_mode)

    outcome: Literal["fetched", "reused", "refreshed"] = "fetched"
    artifact_id = await catalog_client.claim_aggregated_bar_artifact(
        identity=identity,
        worker_id=_WORKER_ID,
        lease_ttl_ms=_LEASE_TTL_MS,
        data_contract_hash=dch,
        file_path=file_path,
    )
    lease_generation = catalog_client.INITIAL_LEASE_GENERATION
    if artifact_id is None:
        existing = await catalog_client.select_complete_aggregated_bar_artifact(identity)
        if existing is not None:
            if existing.data_contract_hash == dch:
                return existing, None, "reused"  # cache hit — same source set
            # The symbol's catalogued minute coverage has grown (or a source
            # minute artifact's bytes changed under a day-refresh) since this
            # daily artifact was last built. Rebuild it onto the current full
            # set instead of refusing — see catalog_client.refresh_complete_artifact.
            prior = await catalog_client.refresh_complete_artifact(
                artifact_id=existing.id,
                worker_id=_WORKER_ID,
                lease_ttl_ms=_LEASE_TTL_MS,
            )
            if prior is None:
                # Raced with another worker's own refresh/claim between the two
                # selects above; the caller's next ensure_data call retries.
                return (
                    None,
                    ArtifactFailure(
                        artifact_kind=identity.artifact_kind,
                        symbol=identity.symbol,
                        trading_date=None,
                        data_type=identity.data_type,
                        reason="lease_timeout",
                        detail="daily-trade rebuild raced with another worker; retry on a later ensure_data call",
                        attempt_count=1,
                    ),
                    "fetched",
                )
            artifact_id = existing.id
            lease_generation = prior.new_lease_generation
            outcome = "refreshed"
        else:
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
                "fetched",
            )

    # Read all source trade bars from disk.
    all_bars: list[MinuteTradeBar] = []
    for src in sorted(source_trade_records, key=lambda r: r.trading_date or spec.start_trading_date):
        try:
            bars = _read_minute_trade_bars(src.file_path, lake_root)
            all_bars.extend(bars)
        except Exception as e:
            # A rebuild (outcome == "refreshed") that fails here hasn't
            # written anything new yet — restore the previously-complete
            # artifact rather than marking it 'failed' with no retry path
            # (steal_or_retry_minute_bar doesn't cover aggregated-bar rows).
            if outcome == "refreshed":
                await catalog_client.restore_complete_artifact(artifact_id, _WORKER_ID, lease_generation)
            else:
                await catalog_client.fail_artifact(artifact_id, "io_error", str(e), worker_id=_WORKER_ID, lease_generation=lease_generation)
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
                "fetched",
            )

    aggregates = aggregate_minute_to_daily(all_bars)
    payload = build_daily_zip_bytes(symbol=identity.symbol or "", aggregates=aggregates)
    staging_root = resolve_staging_root(spec.price_adjustment_mode)
    row_count = len(aggregates)
    file_sha, lease_failure = await _publish_under_lease(
        identity=identity,
        payload=payload,
        lake_root=lake_root,
        staging_root=staging_root,
        rel_path=rel_path,
        spec=spec,
        artifact_id=artifact_id,
        lease_generation=lease_generation,
        trading_date=None,
        row_count=row_count,
        first_bar_start_ms=0,
        last_bar_start_ms=0,
        # Rebuild path (outcome == "refreshed") completes onto a different
        # source set than the row's existing DataContractHash — persist the
        # freshly computed one or the next ensure_data call sees the same
        # stale mismatch and rebuilds again. "fetched" path claims with dch
        # already (claim_aggregated_bar_artifact above), so this is a no-op
        # there, but passing it unconditionally keeps both paths honest.
        data_contract_hash=dch,
    )
    if lease_failure is not None:
        return None, lease_failure, "fetched"
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
            file_size_bytes=len(payload),
            data_root_id=identity.data_root_id,
        ),
        None,
        outcome,
    )


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def _metadata_bootstrap_detail(kind_label: str, detail: str | None) -> str:
    """ArtifactFailure.detail text for a Phase 0 metadata bootstrap failure.

    Keeps the kind-identifying prefix ("market-hours"/"symbol-properties"/
    "interest-rate") existing callers match on, while appending the real
    diagnostic message from ``MetadataBootstrap.detail`` when one is
    available — naming the launcher explicitly when it's the cause (e.g.
    "launcher at http://...:8090 unreachable: ...") instead of only the
    generic "see launcher logs" (#1889).
    """
    if detail:
        return f"{kind_label} metadata bootstrap failed: {detail}"
    return f"{kind_label} metadata bootstrap failed; see launcher logs"


async def ensure_data(spec: DataRunSpec) -> DataAvailabilityResult:
    """Full Slice 1c pipeline: Phase 0 metadata bootstrap + Pass 1 + Pass 2.

    Phase 0: Extract LEAN metadata (market-hours + symbol-properties) from the
    launcher and publish both as lake artifacts. LEAN reads them off the mount
    and refuses to initialize without them. They are not this pipeline's
    calendar -- session enumeration goes through the canonical NYSE calendar
    (``app.lean_sidecar.trading_calendar``, via ``app.data_lake.sessions``).

    Pass 1: Polygon-sourced artifacts — minute-trade, factor_file, map_file.

    Pass 2: Derived artifacts — minute-quote (from same-day trade artifact),
    daily-trade (from all same-symbol trade artifacts). Runs after Pass 1.
    """
    started_ms = int(time.time() * 1000)
    lake_root, staging_root = _writable_lake_roots(spec)
    # LEAN reads the lake as its data folder in sidecar mode, and it expects
    # the corporate-action directories to exist even when a window has no
    # corporate actions. The read-only mount cannot create them at run time,
    # so a writer does it here. Idempotent; see path_policy.
    ensure_lean_readable_layout(lake_root, spec.market)

    # Ensure pool exists. init_pool is idempotent; pool stays alive across calls.
    await catalog_client.init_pool()

    artifacts: list[ArtifactRecord] = []
    failures: list[ArtifactFailure] = []
    fetched_count = 0
    reused_count = 0
    refreshed_count = 0

    # -----------------------------------------------------------------------
    # Phase 0: LEAN metadata bootstrap
    #
    # One call for the whole bundle (#1879, PR C of #1861): a single
    # extraction, an on-disk receipt binding all three files to
    # spec.lean_image_digest, and per-kind catalog activation from that
    # verified receipt -- replacing three independent per-kind bootstrap
    # calls (and three independent launcher round trips) this used to make.
    # -----------------------------------------------------------------------
    mh_outcome, sp_outcome, ir_outcome = await ensure_lean_metadata_bundle(
        spec=spec, lake_root=lake_root, staging_root=staging_root
    )
    mh_record, mh_reused, mh_failure_reason, mh_detail = mh_outcome
    sp_record, sp_reused, sp_failure_reason, sp_detail = sp_outcome
    ir_record, ir_reused, ir_failure_reason, ir_detail = ir_outcome

    # The market-hours database is bootstrapped for LEAN, which reads it off
    # the mount and refuses to initialize without it. It is deliberately NOT
    # this pipeline's calendar: which days are sessions comes from the
    # canonical NYSE calendar via ``trading_sessions_for``, the same one the
    # sidecar's coverage demand and the backfill job's iteration already use.
    if mh_record is not None:
        artifacts.append(mh_record)
        if mh_reused:
            reused_count += 1
        else:
            fetched_count += 1
    else:
        # Bootstrap failure — surface as ArtifactFailure so the run-
        # materialization seam's partial-coverage policy can gate (ADR 0049
        # §3a). Session enumeration is unaffected; what a run loses is the
        # metadata artifact LEAN itself needs.
        failures.append(
            ArtifactFailure(
                artifact_kind="metadata",
                symbol=None,
                trading_date=None,
                data_type=None,
                reason=mh_failure_reason,
                detail=_metadata_bootstrap_detail("market-hours", mh_detail),
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
                reason=sp_failure_reason,
                detail=_metadata_bootstrap_detail("symbol-properties", sp_detail),
                attempt_count=1,
            )
        )

    # Unlike market-hours/symbol-properties, interest-rate is optional: LEAN
    # falls back to its built-in risk-free rate when it's absent (see
    # app.lean_sidecar.lake_mount's module docstring). Confirmed absence
    # ("provider_no_data") is a fact the receipt itself records
    # (``files.interest_rate: null`` -- app.data_lake.metadata_bundle's
    # ``_activate_catalog_from_receipt`` returns this reason directly,
    # without ever attempting a catalog claim for it), so it is non-blocking
    # and, per that module's own trade-off note, stable across repeat calls
    # with the same lean_image_digest rather than retried indefinitely: a
    # digest change is what triggers another extraction attempt. Any other
    # reason (io_error, fetch_timeout, lease_timeout) means the attempt to
    # learn whether interest-rate data exists itself failed or is genuinely
    # racing another worker -- surfacing those exactly like a
    # market-hours/symbol-properties failure is what stops the run from
    # silently claiming input parity it doesn't have (CodeRabbit review fix
    # on #1859, preserved by #1879's bundle rewrite).
    if ir_record is not None:
        artifacts.append(ir_record)
        if ir_reused:
            reused_count += 1
        else:
            fetched_count += 1
    elif ir_failure_reason in ("provider_no_data", "lease_timeout"):
        logger.info(
            "data_lake.ensure_data: interest-rate metadata not available this call "
            "(reason=%s) — non-blocking",
            ir_failure_reason,
        )
    else:
        failures.append(
            ArtifactFailure(
                artifact_kind="metadata",
                symbol=None,
                trading_date=None,
                data_type=None,
                reason=ir_failure_reason,
                detail=_metadata_bootstrap_detail("interest-rate", ir_detail),
                attempt_count=1,
            )
        )

    # -----------------------------------------------------------------------
    # Expand required artifacts (now with real calendar if available)
    # -----------------------------------------------------------------------
    required, non_sessions = expand_required_artifacts(spec)

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
            record, failure, outcome = await _process_factor_file_artifact(
                identity,
                spec,
                minute_trade_by_symbol.get(sym, []),
                lake_root,
            )
            if record is not None:
                artifacts.append(record)
                if outcome == "reused":
                    reused_count += 1
                elif outcome == "refreshed":
                    refreshed_count += 1
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
            # Symbol-wide, not window-scoped: the daily artifact's job is to
            # always reflect everything currently catalogued for this
            # symbol, so its source set is read from the catalog directly
            # rather than from this call's own required-artifacts window
            # (minute_trade_by_symbol). This is what makes a second,
            # differently-windowed ensure_data call for the same symbol a
            # legitimate rebuild instead of a data_contract_mismatch — see
            # _process_daily_trade_artifact.
            source_records = await catalog_client.select_coverage_minute_bars(
                spec.market,
                sym,
                "trade",
                None,
                None,
                price_adjustment_mode=spec.price_adjustment_mode,
            )
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
            record, failure, outcome = await _process_daily_trade_artifact(identity, source_records, spec, lake_root)
            if record is not None:
                artifacts.append(record)
                if outcome == "reused":
                    reused_count += 1
                elif outcome == "refreshed":
                    refreshed_count += 1
                else:
                    fetched_count += 1
            elif failure is not None:
                failures.append(failure)

    overall_status = classify_overall_status(has_failures=bool(failures), has_success=bool(artifacts))

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
        refreshed_artifact_count=refreshed_count,
        completed_at_ms=completed_ms,
        duration_ms=completed_ms - started_ms,
    )
