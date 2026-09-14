"""Default-off, Compose-only ASGI qualification hooks for fleet lane capacity.

The hooks and their SDK/feed injection exist only for an enrolled clerk in the
random namespace minted by the host qualification ceremony. They expose no
execution operation, and every hook request carries that ceremony's in-memory
secret.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from app.broker.alpaca.broker import AlpacaBroker
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import AlpacaSettings
from app.broker.alpaca.market_liveness import (
    AlpacaMarketLivenessConsumer,
    get_market_liveness_consumer,
    read_shared_market_status,
    set_market_liveness_consumer,
)
from app.broker.capture.journal import get_capture_journal
from app.broker.contract.registry import get_broker_registry
from app.config import settings as service_settings
from app.schemas.market_liveness import MarketStatusSnapshot
from app.services.market_liveness import get_market_liveness_store
from app.utils.timestamps import now_ms_utc

_PREFIX = "/internal/fleet-qualification"
_NAMESPACE_PREFIX = "compose:fleetqualification"
_STATUS_SNAPSHOT_PATH = "/api/brokers/alpaca/market-status-snapshot"


class QualificationMarketDependencyAvailable(BaseModel):
    """Successful supported-status dependency evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["alpaca_market_status_consumer"]


class QualificationMarketDependencyUnavailable(BaseModel):
    """Typed absence of the supported status dependency."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: Literal["qualification_market_status_unavailable"]


class QualificationHoldRequestResponse(BaseModel):
    """Typed acknowledgement for the bounded ASGI contention probe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    held: Literal["request"]


def _is_qualification_lane(
    *,
    role: str,
    namespace: str,
    secret: str,
    broker_url: str,
    market_data_url: str,
) -> bool:
    """Keep all fake-provider wiring behind the random Compose ceremony gate."""
    return (
        role == "clerk_agent"
        and namespace.startswith(_NAMESPACE_PREFIX)
        and bool(secret)
        and bool(broker_url)
        and bool(market_data_url)
    )


def _qualification_settings(account_mode: str) -> AlpacaSettings:
    """Return inert, mode-consistent settings for the test-only SDK client."""
    values: dict[str, object] = {
        "api_key_id": "qualification-key",
        "api_secret_key": "qualification-secret",
        "mode": account_mode,
    }
    if account_mode == "live":
        values.update(
            {
                "live_loss_fraction": 0.01,
                "live_loss_usd": 100.0,
                "live_shadow_sessions": 1,
                "live_arming_max_sessions": 1,
                "live_xh_entry_bps": 0.0,
                "live_xh_exit_bps": 0.0,
            }
        )
    return AlpacaSettings.model_validate(values)


def install_qualification_bindings(
    *,
    role: str,
    namespace: str,
    secret: str,
    broker_url: str,
    market_data_url: str,
    account_mode: str,
) -> bool:
    """Inject real Alpaca read/status seams only in a qualifying lane.

    The production registry remains code-owned and unchanged.  This replaces
    the normal lane's already-registered Alpaca *port* only after the strict
    Compose qualification gate has admitted a random namespace.  Its client
    is the production SDK wrapper with Alpaca's supported ``url_override``
    test seam; neither credential values nor mutation paths are enabled.
    """
    if not _is_qualification_lane(
        role=role,
        namespace=namespace,
        secret=secret,
        broker_url=broker_url,
        market_data_url=market_data_url,
    ):
        return False
    if account_mode not in {"paper", "live"}:
        raise ValueError("Qualification account mode must be paper or live.")

    from alpaca.trading.client import TradingClient

    alpaca_settings = _qualification_settings(account_mode)

    def client_factory() -> TradingClient:
        return TradingClient(
            api_key="qualification-key",
            secret_key="qualification-secret",
            paper=account_mode == "paper",
            raw_data=True,
            url_override=broker_url,
        )

    broker = AlpacaBroker(
        client=AlpacaTradingClient(settings=alpaca_settings, client_factory=client_factory),
        settings=alpaca_settings,
    )
    get_broker_registry().register(broker)

    # Alpaca's shared status source is a supported Paper-only topology.  The
    # Live lane still qualifies the declared account read through its own SDK
    # client, but never borrows Paper's status feed.
    if account_mode == "paper":
        async def status_snapshot_source() -> MarketStatusSnapshot:
            return await read_shared_market_status(
                f"{market_data_url}{_STATUS_SNAPSHOT_PATH}",
                control_secret=service_settings.DATA_PLANE_CONTROL_SECRET,
                journal=get_capture_journal(),
            )

        consumer = AlpacaMarketLivenessConsumer(
            read=broker,
            frame_source=_unused_status_frames,
            status_snapshot_source=status_snapshot_source,
        )
        set_market_liveness_consumer(consumer)
    return True


async def _unused_status_frames() -> AsyncIterator[bytes]:
    """Satisfy the consumer's frame-source contract for its snapshot mode."""
    if False:
        yield b""


def qualification_router(
    *, role: str, namespace: str, secret: str, broker_url: str, market_data_url: str
) -> APIRouter | None:
    """Build the non-production hook surface only for a qualifying clerk role."""
    if not _is_qualification_lane(
        role=role,
        namespace=namespace,
        secret=secret,
        broker_url=broker_url,
        market_data_url=market_data_url,
    ):
        return None
    router = APIRouter(prefix=_PREFIX, include_in_schema=False)

    def require_secret(value: str | None) -> None:
        if value != secret:
            raise HTTPException(status_code=404, detail="Not found")

    @router.get(
        "/dependency/market-data",
        response_model=QualificationMarketDependencyAvailable,
        responses={503: {"model": QualificationMarketDependencyUnavailable}},
    )
    async def market_dependency(
        x_fleet_qualification_secret: str | None = Header(default=None),
    ) -> QualificationMarketDependencyAvailable | JSONResponse:
        """Refresh the supported Paper status consumer before its normal read route."""
        require_secret(x_fleet_qualification_secret)
        consumer = get_market_liveness_consumer()
        if consumer is None:
            return JSONResponse(
                status_code=503,
                content=QualificationMarketDependencyUnavailable(
                    reason="qualification_market_status_unavailable"
                ).model_dump(),
            )
        await consumer.refresh_shared_status()
        snapshot = get_market_liveness_store().status_snapshot(now_ms=now_ms_utc())
        if not snapshot.connected:
            return JSONResponse(
                status_code=503,
                content=QualificationMarketDependencyUnavailable(
                    reason="qualification_market_status_unavailable"
                ).model_dump(),
            )
        return QualificationMarketDependencyAvailable(source="alpaca_market_status_consumer")

    @router.get("/hold/request", response_model=QualificationHoldRequestResponse)
    async def hold_request(
        x_fleet_qualification_secret: str | None = Header(default=None),
    ) -> QualificationHoldRequestResponse:
        """Hold one ordinary ASGI request long enough for deployed admission to contend."""
        require_secret(x_fleet_qualification_secret)
        await asyncio.sleep(1)
        return QualificationHoldRequestResponse(held="request")

    @router.get("/hold/stream")
    async def hold_stream(x_fleet_qualification_secret: str | None = Header(default=None)) -> StreamingResponse:
        """Hold one SSE response long enough for the deployed stream pool to contend."""
        require_secret(x_fleet_qualification_secret)

        async def events() -> AsyncIterator[bytes]:
            await asyncio.sleep(1)
            yield b"data: qualification\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return router


def qualification_router_from_environment(role: str, namespace: str) -> APIRouter | None:
    """Resolve gated values without making fake upstreams a runtime registry concern."""
    return qualification_router(
        role=role,
        namespace=namespace,
        secret=os.environ.get("FLEET_QUALIFICATION_PROBE_SECRET", ""),
        broker_url=os.environ.get("FLEET_FAKE_BROKER_URL", ""),
        market_data_url=os.environ.get("FLEET_FAKE_MARKET_DATA_URL", ""),
    )


def install_qualification_bindings_from_environment(role: str, namespace: str) -> bool:
    """Install qualification-only client/feed injection after normal registration."""
    return install_qualification_bindings(
        role=role,
        namespace=namespace,
        secret=os.environ.get("FLEET_QUALIFICATION_PROBE_SECRET", ""),
        broker_url=os.environ.get("FLEET_FAKE_BROKER_URL", ""),
        market_data_url=os.environ.get("FLEET_FAKE_MARKET_DATA_URL", ""),
        account_mode=os.environ.get("FLEET_QUALIFICATION_ACCOUNT_MODE", ""),
    )


__all__ = [
    "install_qualification_bindings",
    "install_qualification_bindings_from_environment",
    "qualification_router",
    "qualification_router_from_environment",
]
