"""Evening observations of vendor reference values, never canonical Greeks."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.admission import read_committed_bytes
from app.data_lake.data_contract import data_contract_hash
from app.data_lake.option_lake import (
    LEASE_TTL_MS,
    OPTION_MARKET,
    OPTION_PROVIDER,
    WORKER_ID,
    OptionCaptureResult,
    OptionDataFailure,
    claim_or_take_over,
    publish_option_artifact,
    stored_file_still_reads,
    vendor_request_gate,
)
from app.data_lake.polygon_fetcher import PolygonFetchError
from app.data_lake.polygon_option_http import get_json_object, require_confirmed_status
from app.data_lake.types import ArtifactIdentity
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms

SNAPSHOT_RECIPE_VERSION = 1


class ReferenceMetric(BaseModel):
    value: float | int | None = None
    state: Literal["available", "missing", "unavailable", "invalid"]
    vendor_time_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)


class ContractObservation(BaseModel):
    ticker: str
    open_interest: ReferenceMetric
    implied_volatility: ReferenceMetric
    delta: ReferenceMetric
    gamma: ReferenceMetric
    theta: ReferenceMetric
    vega: ReferenceMetric


class ChainObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe_version: Literal[1] = SNAPSHOT_RECIPE_VERSION
    underlying: str
    session_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    observation_id: str
    started_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    ended_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    complete: bool
    reason: str | None
    page_receipts: list[str]
    contracts: list[ContractObservation]
    field_gaps: dict[str, int]
    reference_label: str = "Vendor observation; open interest is not verified as this session's settled figure."


def _metric(source: dict[str, Any], name: str, *, integer: bool = False, timestamp: str | None = None) -> ReferenceMetric:
    if name not in source:
        return ReferenceMetric(state="missing")
    value = source[name]
    if value is None:
        return ReferenceMetric(state="unavailable")
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) or not math.isfinite(value):
        return ReferenceMetric(state="invalid")
    if integer and (value < 0 or value != int(value)):
        return ReferenceMetric(state="invalid")
    vendor_time = source.get(timestamp) if timestamp is not None else None
    # Only a timestamp on this metric's own nested object is evidence. The
    # vendor declares last_updated in nanoseconds. Bars/quotes are unrelated.
    ms = vendor_time // 1_000_000 if isinstance(vendor_time, int) and not isinstance(vendor_time, bool) else None
    if ms is not None and not 0 <= ms <= MAX_TIMESTAMP_MS:
        ms = None
    return ReferenceMetric(value=int(value) if integer else float(value), state="available", vendor_time_ms=ms)


def _contract(row: dict[str, Any]) -> ContractObservation:
    details = row.get("details")
    ticker = details.get("ticker") if isinstance(details, dict) else None
    if not isinstance(ticker, str) or not ticker.startswith("O:"):
        raise ValueError("snapshot row has no OCC ticker")
    greeks = row.get("greeks")
    nested = greeks if isinstance(greeks, dict) else {}
    return ContractObservation(
        ticker=ticker.removeprefix("O:"), open_interest=_metric(row, "open_interest", integer=True),
        implied_volatility=_metric(row, "implied_volatility"),
        **{field: _metric(nested, field, timestamp="last_updated") for field in ("delta", "gamma", "theta", "vega")},
    )


async def observe_chain(
    underlying: str, session: date, *, clock: Callable[[], int], max_pages: int = 100,
    should_pause: Callable[[], bool] = lambda: False,
) -> ChainObservation:
    started = clock()
    path = f"/v3/snapshot/options/{underlying}"
    url = "https://api.polygon.io" + path
    next_url: str | None = url
    seen: set[str] = set()
    contracts: dict[str, ContractObservation] = {}
    page_receipts: list[str] = []
    reason: str | None = None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            while next_url is not None:
                cursor = httpx.URL(next_url)
                if (cursor.scheme != "https" or cursor.host != "api.polygon.io" or cursor.path != path
                        or "apiKey" in cursor.params or cursor.userinfo or cursor.fragment):
                    raise ValueError("snapshot cursor left its authenticated endpoint")
                if next_url in seen or len(seen) >= max_pages:
                    raise ValueError("snapshot paging repeated a cursor or exceeded its page bound")
                if should_pause():
                    raise ValueError("snapshot paused before starting the next request")
                if et_date_at_ms(clock()) != session:
                    raise ValueError("snapshot crossed the capture-session date")
                seen.add(next_url)
                async with vendor_request_gate():
                    if should_pause():
                        raise ValueError("snapshot paused while waiting for a request slot")
                    body = await get_json_object(client, cursor, params={"limit": 250} if next_url == url else None,
                                                 api_key=settings.POLYGON_API_KEY, label=underlying)
                require_confirmed_status(body, underlying, 200)
                rows = body.get("results")
                if not isinstance(rows, list):
                    raise ValueError("snapshot page has no results list")
                page_receipts.append(hashlib.sha256(json.dumps(body, default=str, sort_keys=True).encode()).hexdigest())
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError("snapshot contains a malformed contract")
                    contract = _contract(row)
                    if contract.ticker in contracts:
                        raise ValueError("snapshot contains a duplicate contract")
                    contracts[contract.ticker] = contract
                cursor_value = body.get("next_url")
                if cursor_value is not None and (not isinstance(cursor_value, str) or not cursor_value or not rows):
                    raise ValueError("snapshot has a missing or malformed page")
                next_url = cursor_value
    except (PolygonFetchError, ValueError) as exc:
        reason = str(exc)
    gaps = Counter(f"{name}:{metric.state}" for contract in contracts.values()
                   for name, metric in contract if isinstance(metric, ReferenceMetric) and metric.state != "available")
    return ChainObservation(
        underlying=underlying, session_ms=session_open_ms_utc(session), observation_id=uuid4().hex,
        started_ms=started, ended_ms=clock(), complete=reason is None, reason=reason,
        page_receipts=page_receipts, contracts=sorted(contracts.values(), key=lambda item: item.ticker), field_gaps=dict(gaps),
    )


def snapshot_path(observation: ChainObservation) -> PurePosixPath:
    return PurePosixPath("usa_options") / observation.underlying.lower() / "observations" / str(observation.session_ms) / f"{observation.observation_id}.json"


def snapshot_recipe(underlying: str, session_ms: int, observation_id: str) -> str:
    return data_contract_hash(provider=OPTION_PROVIDER, provider_params={"endpoint": "option_chain_snapshot",
        "underlying": underlying, "session_ms": session_ms, "observation_id": observation_id, "recipe": SNAPSHOT_RECIPE_VERSION},
        price_adjustment_mode="raw", session_policy="observation", lean_format_version=1)


async def save_observation(observation: ChainObservation) -> OptionCaptureResult:
    day = et_date_at_ms(observation.session_ms)
    rel_path = snapshot_path(observation)
    recipe = snapshot_recipe(observation.underlying, observation.session_ms, observation.observation_id)
    identity = ArtifactIdentity(artifact_kind="metadata", market=OPTION_MARKET, symbol=observation.underlying,
                                provider=OPTION_PROVIDER, price_adjustment_mode="raw")
    lease = await claim_or_take_over(
        claim=lambda: catalog_client.claim_metadata_artifact(identity, WORKER_ID, LEASE_TTL_MS, recipe, str(rel_path)),
        select_complete=lambda: catalog_client.select_complete_metadata_artifact(recipe),
        read_claim_state=lambda: catalog_client.select_metadata_claim_state(recipe),
        stored_file_reads=lambda row: stored_file_still_reads(row, rel_path, ChainObservation.model_validate_json),
        data_contract_hash=recipe, symbol=observation.underlying, trading_date=day,
    )
    if isinstance(lease, OptionCaptureResult):
        return lease
    payload = observation.model_dump_json().encode()
    result = await publish_option_artifact(payload=payload, rel_path=rel_path, lease=lease,
        row_count=len(observation.contracts), first_bar_start_ms=0, last_bar_start_ms=0,
        data_contract_hash=recipe, symbol=observation.underlying, trading_date=day)
    if isinstance(result, OptionDataFailure):
        return OptionCaptureResult.failed(result)
    row = await catalog_client.select_complete_metadata_artifact(recipe)
    if row is None:
        raise RuntimeError("published snapshot has no committed receipt")
    return OptionCaptureResult(record=row, failure=None)


async def read_observation(path: Path) -> ChainObservation:
    payload = await asyncio.to_thread(read_committed_bytes, path)
    return ChainObservation.model_validate_json(payload)
