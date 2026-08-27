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

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.broker.ibkr import contracts as ibkr_contracts
from app.broker.ibkr.api_evidence import (
    IbkrApiEvidenceEvent,
    get_ibkr_api_evidence_recorder,
)
from app.broker.ibkr.auto_reconnect_monitor import get_monitor
from app.broker.ibkr.bar_models import IbkrBarsSnapshot
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
from app.broker.ibkr.contracts import search_option_contracts
from app.broker.ibkr.health import (
    build_broker_health,
    synthetic_disconnected_health,
)
from app.broker.ibkr.market_data import stream_option_chain
from app.broker.ibkr.models import (
    DataPlaneHealth,
    IbkrChainSnapshot,
    IbkrConnectionHealth,
    IbkrStrikeList,
    IbkrSurfaceSnapshot,
)
from app.broker.ibkr.surface import (
    DEFAULT_MAX_LINES as SURFACE_DEFAULT_MAX_LINES,
)
from app.broker.ibkr.surface import (
    stream_option_surface,
)
from app.routers.broker_dependencies import is_broker_disabled, require_connected_client
from app.schemas.broker_search import OptionContractMatch
from app.services.data_plane_health import data_plane_health
from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR
from app.utils.throttle import TtlCache

router = APIRouter(prefix="/api/broker", tags=["broker"])
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


# Slice 1F — option-contracts drill-down 300s response cache. Cache key is
# ``(symbol, expiry_ms, strike, right)`` (qualification is heavy but not
# rate-limited upstream, unlike the retired symbol-search proxy).
_OPTION_CONTRACTS_CACHE: TtlCache[
    tuple[str, int, float, str], list[OptionContractMatch]
] = TtlCache(ttl_seconds=300.0, max_size=512)


def _ibkr_api_evidence_to_sse(event: IbkrApiEvidenceEvent) -> str:
    return f"event: ibkr_api\ndata: {event.model_dump_json()}\n\n"


def reset_broker_search_state_for_testing() -> None:
    """Test-only hook — flush the TTL cache so an earlier test cannot leak
    a cached response into the next assertion."""
    global _OPTION_CONTRACTS_CACHE
    _OPTION_CONTRACTS_CACHE = TtlCache(ttl_seconds=300.0, max_size=512)


# ── /ibkr/evidence diagnostics ────────────────────────────────────────


@router.get("/ibkr/evidence", response_model=list[IbkrApiEvidenceEvent])
async def ibkr_api_evidence_backfill(
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 250,
) -> list[IbkrApiEvidenceEvent]:
    """Recent raw IBKR API evidence captured at broker adapter boundaries."""
    return get_ibkr_api_evidence_recorder().backfill(after_seq=after_seq, limit=limit)


@router.get("/ibkr/evidence/stream")
async def ibkr_api_evidence_stream(
    since_seq: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """SSE stream of raw IBKR API evidence for cockpit diagnostics."""
    recorder = get_ibkr_api_evidence_recorder()

    async def event_source():
        for event in recorder.backfill(after_seq=since_seq, limit=500):
            yield _ibkr_api_evidence_to_sse(event)
        subscription = recorder.subscribe()
        try:
            while True:
                event = await subscription.queue.get()
                if event is None:
                    break
                if event.seq > since_seq:
                    yield _ibkr_api_evidence_to_sse(event)
        except asyncio.CancelledError:
            raise
        finally:
            recorder.unsubscribe(subscription)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── /health ────────────────────────────────────────────────────────────


@router.get("/data-plane/health", response_model=DataPlaneHealth)
async def data_plane_health_endpoint() -> DataPlaneHealth:
    """Code-liveness diagnostic for the long-running FastAPI data plane."""
    return data_plane_health()


@router.get("/health", response_model=IbkrConnectionHealth)
async def broker_health() -> IbkrConnectionHealth:
    """Connection diagnostic. Never raises on disconnect."""
    if is_broker_disabled():
        return synthetic_disconnected_health(
            state="disabled",
            disabled=True,
            reason="IBKR_BROKER_ENABLED=false — host-venv runner owns the IBKR session",
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


# ── /expirations ───────────────────────────────────────────────────────


@router.get("/expirations/{symbol}")
async def list_expirations_endpoint(symbol: str) -> dict:
    client = require_connected_client()
    try:
        expirations = await ibkr_contracts.list_expirations(client, symbol.upper())
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"symbol": symbol.upper(), "expirations_ms": expirations}


# ── /strikes ───────────────────────────────────────────────────────────


@router.get("/strikes/{symbol}", response_model=IbkrStrikeList)
async def list_strikes_endpoint(
    symbol: str,
    expiry_ms: Annotated[int, Query(..., gt=0, description="Expiry timestamp in int64 ms UTC.")],
) -> IbkrStrikeList:
    """Strikes that IBKR can actually qualify for one (symbol, expiry).

    Filters the raw ``reqSecDefOptParams`` payload by probing
    ``qualifyContractsAsync`` per strike, so the response carries only
    strikes the chain stream will accept without partial-qualification
    rejection.
    """
    from datetime import UTC, datetime

    client = require_connected_client()
    sym = symbol.upper()
    try:
        strikes = await ibkr_contracts.list_qualified_strikes(client, sym, expiry_ms)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return IbkrStrikeList(
        symbol=sym,
        expiry_ms=expiry_ms,
        strikes=strikes,
        fetched_at_ms=int(datetime.now(tz=UTC).timestamp() * 1000),
    )


# ── /option-contracts/{symbol} ─────────────────────────────────────────


@router.get("/option-contracts/{symbol}")
async def option_contracts_endpoint(
    symbol: str,
    expiry_ms: Annotated[int, Query(gt=0, description="Expiry timestamp in int64 ms UTC.")],
    strike: Annotated[float, Query(gt=0, description="Option strike.")],
    right: Annotated[str, Query(pattern="^[CP]$", description="C for call, P for put.")],
) -> dict:
    """Slice 1F — IBKR ``reqContractDetails`` qualification for the
    cockpit's option leg picker. Returns the rich ``OptionContractMatch``
    (with ``con_id``, ``local_symbol``, etc.) that the picker persists
    alongside the declared leg. ``conId`` is the broker-canonical
    identity the Slice 4 resolver will key against.
    """
    client = require_connected_client()
    sym = symbol.upper()
    key = (sym, expiry_ms, float(strike), right)
    cached = _OPTION_CONTRACTS_CACHE.get(key)
    if cached is not None:
        return {"matches": [m.model_dump() for m in cached]}

    try:
        matches = await search_option_contracts(
            client,
            symbol=sym,
            expiry_ms=expiry_ms,
            strike=float(strike),
            right=right,  # type: ignore[arg-type]
        )
    except NotConnectedError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "IBKR client not connected.",
        ) from exc

    _OPTION_CONTRACTS_CACHE.set(key, matches)
    return {"matches": [m.model_dump() for m in matches]}


# ── /option-chain (SSE) ────────────────────────────────────────────────


@router.get("/option-chain/{symbol}")
async def option_chain_stream(
    symbol: str,
    expiry_ms: Annotated[int, Query(..., gt=0, description="Expiry timestamp in int64 ms UTC.")],
    strikes: Annotated[
        list[float] | None,
        Query(
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
    if any(k <= 0 for k in strikes):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Strikes must be positive.",
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
            logger.error("Broker error in option-chain stream: %s", exc)
            yield _sse_error_frame(
                "The option-chain stream stopped: the broker rejected or dropped the market-data request. The broker's "
                "reason is in the service log."
            )
        except ValueError as exc:
            # Contract qualification (``qualify_underlying``,
            # ``build_option_contract``) raises ValueError when IBKR
            # cannot resolve a symbol/strike/right combination — surface
            # those through the same SSE error path as broker errors.
            logger.error("Invalid option-chain request: %s", exc)
            yield _sse_error_frame(
                "The option-chain request could not be qualified. Check the symbol, expiry, strike, and right."
            )

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── /option-surface (SSE) ──────────────────────────────────────────────


@router.get("/option-surface/{symbol}")
async def option_surface_stream(
    symbol: str,
    expiry_ms: Annotated[
        list[int] | None,
        Query(
            description=(
                "Expirations to fan over (repeated). Each value is an int64 "
                "ms UTC timestamp from /api/broker/expirations/{symbol}."
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
    if any(e <= 0 for e in expiry_ms):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "expiry_ms entries must be positive.",
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
            logger.error("Broker error in option-surface stream: %s", exc)
            yield _sse_error_frame(
                "The option-surface stream stopped: the broker rejected or dropped the market-data request. The "
                "broker's reason is in the service log."
            )
        except ValueError as exc:
            logger.error("Invalid option-surface request: %s", exc)
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


@router.get("/bars/snapshot", response_model=IbkrBarsSnapshot)
async def bars_snapshot_endpoint(
    symbol: Annotated[str, Query(min_length=1, max_length=12)],
    since_ms: Annotated[int | None, Query(ge=0)] = None,
) -> IbkrBarsSnapshot:
    """Return the live 1-min OHLCV buffer for ``symbol``.

    Idempotent: first call lazily subscribes to ``reqRealTimeBars`` on the
    public broker session; subsequent calls return the current buffer.
    ``since_ms`` filters bars to ``start_ms > since_ms`` for incremental
    polling. ``status`` reflects subscription health so the UI can
    distinguish "no bars yet" (subscribing) from "broker disconnected"
    (errored).
    """
    _raise_if_disabled()
    sym = symbol.strip().upper()
    state = await LIVE_BAR_AGGREGATOR.ensure_subscribed(sym)
    bars = LIVE_BAR_AGGREGATOR.snapshot(sym, since_ms=since_ms)
    return IbkrBarsSnapshot(
        symbol=sym,
        status=state.status,
        last_error=state.last_error,
        last_bar_ms=state.last_bar_ms,
        bars=bars,
    )


@router.get("/bars-5s/snapshot", response_model=IbkrBarsSnapshot)
async def bars_5s_snapshot_endpoint(
    symbol: Annotated[str, Query(min_length=1, max_length=12)],
    since_ms: Annotated[int | None, Query(ge=0)] = None,
) -> IbkrBarsSnapshot:
    """Return the live raw 5-sec OHLCV buffer for ``symbol``.

    Mirror of ``/bars/snapshot`` for the high-resolution chart. It owns an
    independent 5-second buffer, but same-symbol 5-second and 1-minute
    consumers multiplex onto one public-client ``reqRealTimeBars`` request.
    Each bar's ``end_ms - start_ms`` window is 5 000.
    """
    _raise_if_disabled()
    sym = symbol.strip().upper()
    state = await LIVE_BAR_AGGREGATOR.ensure_subscribed_5s(sym)
    bars = LIVE_BAR_AGGREGATOR.snapshot_5s(sym, since_ms=since_ms)
    return IbkrBarsSnapshot(
        symbol=sym,
        status=state.status,
        last_error=state.last_error,
        last_bar_ms=state.last_bar_ms,
        bars=bars,
    )


# ── helpers ────────────────────────────────────────────────────────────


def _snapshot_to_json(snapshot: IbkrChainSnapshot) -> str:
    return snapshot.model_dump_json()


def _surface_snapshot_to_json(snapshot: IbkrSurfaceSnapshot) -> str:
    return snapshot.model_dump_json()
