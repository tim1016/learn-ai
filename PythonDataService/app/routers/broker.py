"""Public broker endpoints (curated subset).

This is the *only* place outside ``app.broker.*`` that touches IBKR.
The .NET backend and Angular frontend reach IBKR via these endpoints —
no tight coupling to ``ib_async`` types crosses this boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status
from fastapi.responses import StreamingResponse

from app.broker.contract.models import US_EQUITY_SYMBOL_PATTERN
from app.broker.ibkr import contracts as ibkr_contracts
from app.broker.ibkr.auto_reconnect_monitor import get_monitor
from app.broker.ibkr.client import (
    BrokerError,
    ConnectionRefusedDueToSentinelError,
    IbkrClient,
    IbkrClientIdInUseError,
    NotConnectedError,
    get_client,
    get_client_lifecycle_lock,
    set_client,
)
from app.broker.ibkr.contracts import OptionQualificationError
from app.broker.ibkr.health import (
    build_broker_health,
    synthetic_disconnected_health,
)
from app.broker.ibkr.market_data import stream_option_chain
from app.broker.ibkr.models import (
    IbkrChainSnapshot,
    IbkrConnectionHealth,
    IbkrExpirationsResponse,
    IbkrStrikeList,
    IbkrSurfaceSnapshot,
)
from app.broker.ibkr.surface import (
    DEFAULT_MAX_LINES as SURFACE_DEFAULT_MAX_LINES,
)
from app.broker.ibkr.surface import (
    stream_option_surface,
)
from app.options.expiry import OptionExpiryMs
from app.routers.broker_dependencies import is_broker_disabled, require_connected_client
from app.utils.timestamps import now_ms_utc

router = APIRouter(prefix="/api/broker", tags=["broker"])
read_router = APIRouter(tags=["broker"])
logger = logging.getLogger(__name__)

# Serialises POST /connect | /disconnect | /reconnect against two concurrent
# operator clicks AND against the AutoReconnectMonitor's reconnect attempts.
# The lock lives in ``app.broker.ibkr.client`` so both this router and the
# monitor share the same object — without that, a monitor tick could race an
# operator's manual reconnect and double-call ``ib_async.IB.connectAsync``.
_lifecycle_lock = get_client_lifecycle_lock()

# Factory indirection so tests can monkeypatch the client constructor
# without having ``ib_async`` installed. Production callers see the real
# class; tests substitute a fake.
_ibkr_client_factory: type[IbkrClient] = IbkrClient


# ── /health ────────────────────────────────────────────────────────────


@read_router.get("/health", response_model=IbkrConnectionHealth)
async def broker_health() -> IbkrConnectionHealth:
    """Connection diagnostic. Never raises on disconnect."""
    if is_broker_disabled():
        return synthetic_disconnected_health(
            state="disabled",
            disabled=True,
            reason="IBKR_BROKER_ENABLED=false — the data-plane IBKR client is switched off",
        )
    try:
        client = get_client()
    except NotConnectedError:
        return synthetic_disconnected_health()
    return build_broker_health(client, get_monitor())


# ── /connect | /disconnect | /reconnect ────────────────────────────────


@router.post("/connect", response_model=IbkrConnectionHealth)
async def connect_endpoint() -> IbkrConnectionHealth:
    """Establish (or confirm) the IBKR connection. Idempotent.

    Returns the current health if already connected — no second
    ``connectAsync`` is issued. Serialised against /disconnect and
    /reconnect via a process-wide asyncio lock.
    """
    _raise_if_disabled()
    async with _lifecycle_lock:
        client = _get_or_create_client()
        # Operator clicked Connect — mark intent so the monitor knows it
        # SHOULD auto-recover from any future drop.
        client.set_desired_connected(True)
        if client.is_connected():
            return build_broker_health(client, get_monitor())
        return await _connect_and_install(client)


@router.post("/disconnect", response_model=IbkrConnectionHealth)
async def disconnect_endpoint() -> IbkrConnectionHealth:
    """Disconnect from IB Gateway / TWS. Idempotent.

    Returns a disconnected health snapshot if there is no client to
    disconnect; otherwise returns the post-disconnect health.
    """
    _raise_if_disabled()
    async with _lifecycle_lock:
        try:
            client = get_client()
        except NotConnectedError:
            return synthetic_disconnected_health()
        # Operator clicked Disconnect — clear intent so the monitor stops
        # auto-reconnecting against the operator's stated wish (the previous
        # design ignored this and re-connected on the next tick).
        client.set_desired_connected(False)
        await _disconnect_with_error_mapping(client)
        return build_broker_health(client, get_monitor())


@router.post("/reconnect", response_model=IbkrConnectionHealth)
async def reconnect_endpoint() -> IbkrConnectionHealth:
    """Disconnect (if connected) then connect.

    Useful after a Gateway hiccup or after bumping ``IBKR_CLIENT_ID``
    to clear a stale session.
    """
    _raise_if_disabled()
    async with _lifecycle_lock:
        client = _get_or_create_client()
        client.set_desired_connected(True)
        if client.is_connected():
            await _disconnect_with_error_mapping(client)
        return await _connect_and_install(client)


def _raise_if_disabled() -> None:
    if is_broker_disabled():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "IBKR broker is disabled (IBKR_BROKER_ENABLED=false). Cannot drive connection lifecycle.",
        )


def _get_or_create_client() -> IbkrClient:
    try:
        return get_client()
    except NotConnectedError:
        return _ibkr_client_factory()


async def _connect_and_install(client: IbkrClient) -> IbkrConnectionHealth:
    """Call ``client.connect()``, translate errors to HTTPException, install on success."""
    try:
        await client.connect()
    except ConnectionRefusedDueToSentinelError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except IbkrClientIdInUseError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Could not reach IB Gateway: {exc}",
        ) from exc
    set_client(client)
    return build_broker_health(client, get_monitor())


async def _disconnect_with_error_mapping(client: IbkrClient) -> None:
    """Call ``client.disconnect()``, translating socket / broker errors to 502.

    ``IbkrClient.disconnect()`` wraps a sync ``self._ib.disconnect()`` which
    can surface ``OSError`` when the socket teardown races a still-pending
    write. Without this wrapper that bubbles as 500 instead of the
    broker-facing 502 used by every other lifecycle path. ``NotConnectedError``
    is also caught defensively so callers can treat disconnect as idempotent
    even if a future refactor adds a require-connected guard.
    """
    try:
        await client.disconnect()
    except NotConnectedError:
        return
    except (BrokerError, OSError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


_EXPIRY_MS_DESCRIPTION = "An option expiry: 16:00 New York time on the expiry date, as int64 ms UTC."


# ── /expirations ───────────────────────────────────────────────────────


@read_router.get("/expirations/{symbol}", response_model=IbkrExpirationsResponse)
async def list_expirations_endpoint(
    symbol: Annotated[str, Path(pattern=US_EQUITY_SYMBOL_PATTERN)],
) -> IbkrExpirationsResponse:
    client = require_connected_client()
    try:
        expirations = await ibkr_contracts.list_expirations(client, symbol.upper())
    except OptionQualificationError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return IbkrExpirationsResponse(symbol=symbol.upper(), expirations_ms=expirations)


# ── /strikes ───────────────────────────────────────────────────────────


@read_router.get("/strikes/{symbol}", response_model=IbkrStrikeList)
async def list_strikes_endpoint(
    symbol: Annotated[str, Path(pattern=US_EQUITY_SYMBOL_PATTERN)],
    expiry_ms: Annotated[OptionExpiryMs, Query(..., description=_EXPIRY_MS_DESCRIPTION)],
) -> IbkrStrikeList:
    """Strikes that IBKR can actually qualify for one (symbol, expiry).

    Filters the raw ``reqSecDefOptParams`` payload by probing
    ``qualifyContractsAsync`` per strike, so the response carries only
    strikes the chain stream will accept without partial-qualification
    rejection.
    """
    client = require_connected_client()
    sym = symbol.upper()
    try:
        strikes = await ibkr_contracts.list_qualified_strikes(client, sym, expiry_ms)
    except OptionQualificationError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return IbkrStrikeList(
        symbol=sym,
        expiry_ms=expiry_ms,
        strikes=strikes,
        fetched_at_ms=now_ms_utc(),
    )


# ── /option-chain (SSE) ────────────────────────────────────────────────


@read_router.get("/option-chain/{symbol}")
async def option_chain_stream(
    symbol: Annotated[str, Path(pattern=US_EQUITY_SYMBOL_PATTERN)],
    expiry_ms: Annotated[OptionExpiryMs, Query(..., description=_EXPIRY_MS_DESCRIPTION)],
    strikes: Annotated[
        list[float] | None,
        Query(
            max_length=SURFACE_DEFAULT_MAX_LINES // 2,
            description=(
                "Strikes to subscribe. Pick from /api/broker/strikes/{symbol} "
                "so every value is one IBKR can actually qualify."
            ),
        ),
    ] = None,
    debounce_ms: Annotated[int, Query(ge=50, le=5000)] = 250,
) -> StreamingResponse:
    """SSE stream of chain snapshots.

    ``strikes`` is a repeated query parameter — same FastAPI/Pydantic
    encoder bug as ``con_ids`` on the per-position pnl stream, so we
    accept it as optional and 422 explicitly.
    """
    if not strikes:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "strikes must be non-empty.",
        )
    if any((not math.isfinite(k)) or k <= 0 for k in strikes):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Strikes must be finite and positive.",
        )

    client = require_connected_client()
    sym = symbol.upper()
    band = sorted(set(float(k) for k in strikes))

    debounce_seconds = debounce_ms / 1000.0

    async def event_source():
        try:
            async for snapshot in stream_option_chain(client, sym, expiry_ms, band, debounce_seconds=debounce_seconds):
                payload = _snapshot_to_json(snapshot)
                yield f"event: chain\ndata: {payload}\n\n"
        except asyncio.CancelledError:
            raise
        except BrokerError as exc:
            logger.error("Broker error in option-chain stream: %s", exc, exc_info=True)
            yield _sse_error_frame(
                "The option-chain stream stopped: the broker rejected or dropped the market-data request. The broker's "
                "reason is in the service log."
            )
        except OptionQualificationError as exc:
            # Only an expected qualification refusal becomes a client error.
            logger.error("Invalid option-chain request: %s", exc, exc_info=True)
            yield _sse_error_frame(
                "The option-chain request could not be qualified. Check the symbol, expiry, strike, and right."
            )

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── /option-surface (SSE) ──────────────────────────────────────────────


@read_router.get("/option-surface/{symbol}")
async def option_surface_stream(
    symbol: Annotated[str, Path(pattern=US_EQUITY_SYMBOL_PATTERN)],
    expiry_ms: Annotated[
        list[OptionExpiryMs] | None,
        Query(
            description=(
                "Expirations to fan over (repeated). Each value is an option "
                "expiry from /api/broker/expirations/{symbol}: 16:00 New York "
                "time on the expiry date, as int64 ms UTC."
            ),
        ),
    ] = None,
    strikes: Annotated[
        list[float] | None,
        Query(
            description=(
                "Strike band applied at every expiry (repeated). Pick from "
                "/api/broker/strikes/{symbol} so every value qualifies."
            ),
        ),
    ] = None,
    debounce_ms: Annotated[int, Query(ge=50, le=5000)] = 250,
    max_lines: Annotated[
        int,
        Query(
            ge=2,
            le=200,
            description=(
                "Local hard cap on streaming market-data lines. Default 100 "
                "matches IBKR's default user allocation shared across TWS "
                "and all API clients; do not raise without confirming the "
                "username has been granted more."
            ),
        ),
    ] = SURFACE_DEFAULT_MAX_LINES,
) -> StreamingResponse:
    """SSE stream of multi-expiry option-surface snapshots.

    The surface is the same strike band applied to every requested
    expiry, both call and put sides — used by the /broker/options-surface
    page to render the 3D ECharts ``bar3D`` view.
    """
    if not expiry_ms:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "expiry_ms must be non-empty.",
        )
    if not strikes:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "strikes must be non-empty.",
        )
    # Reject NaN/inf as well as non-positive: FastAPI's float coercion
    # accepts them, and propagating either downstream blows up contract
    # qualification with an opaque IBKR error instead of a clean 4xx.
    if any((not math.isfinite(k)) or k <= 0 for k in strikes):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "strikes entries must be finite and positive.",
        )

    client = require_connected_client()
    sym = symbol.upper()
    band = sorted(set(float(k) for k in strikes))
    expiries = sorted(set(int(e) for e in expiry_ms))

    debounce_seconds = debounce_ms / 1000.0

    async def event_source():
        try:
            async for snapshot in stream_option_surface(
                client,
                sym,
                expiries,
                band,
                debounce_seconds=debounce_seconds,
                max_lines=max_lines,
            ):
                payload = _surface_snapshot_to_json(snapshot)
                yield f"event: surface\ndata: {payload}\n\n"
        except asyncio.CancelledError:
            raise
        except BrokerError as exc:
            logger.error("Broker error in option-surface stream: %s", exc, exc_info=True)
            yield _sse_error_frame(
                "The option-surface stream stopped: the broker rejected or dropped the market-data request. The "
                "broker's reason is in the service log."
            )
        except OptionQualificationError as exc:
            logger.error("Invalid option-surface request: %s", exc, exc_info=True)
            yield _sse_error_frame(
                "The option-surface request could not be qualified. Check the symbol, expiry, strike, and right."
            )

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── shared SSE error framing ────────────────────────────────────────────


def _sse_error_frame(message: str) -> str:
    """Serialize one operator-facing SSE error frame.

    The caught exception is logged with its full text at each callsite; only
    this curated sentence crosses the wire. Serializing ``str(exc)`` instead
    hands an external caller whatever an unexpected exception happens to
    carry (CodeQL ``py/stack-trace-exposure``), and the operator gains
    nothing the service log does not already hold.
    """
    return f"event: error\ndata: {json.dumps({'error': message})}\n\n"


# ── helpers ────────────────────────────────────────────────────────────


def _snapshot_to_json(snapshot: IbkrChainSnapshot) -> str:
    return snapshot.model_dump_json()


def _surface_snapshot_to_json(snapshot: IbkrSurfaceSnapshot) -> str:
    return snapshot.model_dump_json()


# Combined posture serves the same read routes beside its manual lifecycle routes.
router.include_router(read_router)
