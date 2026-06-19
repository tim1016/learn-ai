"""Public broker endpoints (curated subset).

This is the *only* place outside ``app.broker.*`` that touches IBKR.
The .NET backend and Angular frontend reach IBKR via these endpoints —
no tight coupling to ``ib_async`` types crosses this boundary.

Endpoints (Phase 1 + Phase 2 + Phase 3):

* ``GET /api/broker/health`` — connection + sentinel.
* ``GET /api/broker/diagnose`` — layered self-test (read-only).
* ``POST /api/broker/connect`` — establish IBKR connection (idempotent).
* ``POST /api/broker/disconnect`` — drop the session (idempotent).
* ``POST /api/broker/reconnect`` — disconnect-then-connect.
* ``GET /api/broker/expirations/{symbol}`` — list expiries.
* ``GET /api/broker/option-chain/{symbol}`` — SSE chain stream.
* ``GET /api/broker/option-surface/{symbol}`` — SSE multi-expiry surface stream.
* ``GET /api/broker/account`` — one-shot account summary (Phase 2a).
* ``GET /api/broker/positions`` — open positions (Phase 2a).
* ``GET /api/broker/pnl/stream`` — account-level P&L SSE (Phase 2b).
* ``GET /api/broker/pnl/positions/stream?con_ids=...`` — per-position P&L SSE.
* ``POST /api/broker/orders`` — place paper order (Phase 3a).
* ``GET /api/broker/orders/open`` — list open orders (Phase 3b).
* ``DELETE /api/broker/orders/{order_id}`` — cancel paper order (Phase 3b).
* ``GET /api/broker/orders/stream`` — order event SSE (Phase 3b).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.broker.ibkr import account as ibkr_account
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
from app.broker.ibkr.contracts import search_option_contracts
from app.broker.ibkr.diagnostics import run_diagnostics
from app.broker.ibkr.health import (
    build_broker_health,
    synthetic_disconnected_health,
)
from app.broker.ibkr.market_data import stream_option_chain
from app.broker.ibkr.models import (
    DiagnosticReport,
    DiagnosticReportDisabled,
    IbkrAccountSummary,
    IbkrBarsSnapshot,
    IbkrChainSnapshot,
    IbkrConnectionHealth,
    IbkrOpenOrder,
    IbkrOrderAck,
    IbkrOrderEvent,
    IbkrOrderSpec,
    IbkrPnLTick,
    IbkrPositionsSnapshot,
    IbkrStrikeList,
    IbkrSurfaceSnapshot,
)
from app.broker.ibkr.orders import (
    OrderNotFoundError,
    OrderRefusedError,
    cancel_paper_order,
    list_open_orders,
    place_paper_order,
    stream_order_events,
)
from app.broker.ibkr.persistence import (
    make_account_writer,
    make_pnl_writer,
    make_writer,
)
from app.broker.ibkr.pnl import (
    DEFAULT_PNL_DEBOUNCE_S,
    stream_account_pnl,
    stream_position_pnl,
)
from app.broker.ibkr.surface import (
    DEFAULT_MAX_LINES as SURFACE_DEFAULT_MAX_LINES,
)
from app.broker.ibkr.surface import (
    stream_option_surface,
)
from app.broker.ibkr.symbol_search import search_symbols
from app.schemas.broker_search import OptionContractMatch, SymbolMatch
from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR
from app.utils.throttle import TokenBucket, TtlCache

router = APIRouter(prefix="/api/broker", tags=["broker"])
logger = logging.getLogger(__name__)

# Computed once at module import — stable for the lifetime of the process.
# Used by DiagnosticReportDisabled.since_ms so each request doesn't generate
# a fresh timestamp that shifts on every poll.
_BROKER_DISABLED_SINCE_MS: int = int(time.time() * 1000)

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


# Slice 1F — broker search throttle + 60s response cache. Token bucket
# is at the IBKR-published ``reqMatchingSymbols`` cadence (~1 req / 5s)
# with a burst of 1 so a single typo does not waste the operator's
# allowance. Cache keys are ``(pattern, sec_type)`` for the symbol
# search and ``(symbol, expiry_ms, strike, right)`` for the option
# drill-down (drill-down qualification is heavy but not rate-limited
# upstream).
_SYMBOL_SEARCH_BUCKET: TokenBucket = TokenBucket(rate_per_second=0.2, capacity=1)
_SYMBOL_SEARCH_CACHE: TtlCache[tuple[str, str | None], list[SymbolMatch]] = TtlCache(
    ttl_seconds=60.0, max_size=256
)
_OPTION_CONTRACTS_CACHE: TtlCache[
    tuple[str, int, float, str], list[OptionContractMatch]
] = TtlCache(ttl_seconds=300.0, max_size=512)


def reset_broker_search_state_for_testing() -> None:
    """Test-only hook — flush the throttle bucket and TTL cache so an
    earlier test cannot starve the next one of tokens."""
    global _SYMBOL_SEARCH_BUCKET, _SYMBOL_SEARCH_CACHE, _OPTION_CONTRACTS_CACHE
    _SYMBOL_SEARCH_BUCKET = TokenBucket(rate_per_second=0.2, capacity=1)
    _SYMBOL_SEARCH_CACHE = TtlCache(ttl_seconds=60.0, max_size=256)
    _OPTION_CONTRACTS_CACHE = TtlCache(ttl_seconds=300.0, max_size=512)


# ── /health ────────────────────────────────────────────────────────────


@router.get("/health", response_model=IbkrConnectionHealth)
async def broker_health() -> IbkrConnectionHealth:
    """Connection diagnostic. Never raises on disconnect."""
    from app.broker.ibkr.config import get_settings
    from app.broker.safety_verdict import derive_broker_safety_verdict

    s = get_settings()
    # Safety verdict is re-derived on every call so it reflects the latest
    # connection state. ADR 0011 §3 — no connect-time cache.
    if _is_broker_disabled():
        return synthetic_disconnected_health(
            state="disabled",
            disabled=True,
            reason="IBKR_BROKER_ENABLED=false — host-venv runner owns the IBKR session",
            safety_verdict=derive_broker_safety_verdict(
                configured_mode=s.mode,
                readonly_flag=None,
                port=s.port,
                connected_account=None,
            ),
        )
    try:
        client = get_client()
    except NotConnectedError:
        return synthetic_disconnected_health(
            safety_verdict=derive_broker_safety_verdict(
                configured_mode=s.mode,
                readonly_flag=None,
                port=s.port,
                connected_account=None,
            ),
        )
    return build_broker_health(
        client,
        get_monitor(),
        safety_verdict=derive_broker_safety_verdict(
            configured_mode=client.settings.mode,
            readonly_flag=None,
            port=client.settings.port,
            connected_account=client.connected_account,
        ),
    )


# ── /diagnose ──────────────────────────────────────────────────────────


@router.get("/diagnose", response_model=DiagnosticReport)
async def broker_diagnose() -> DiagnosticReport:
    """Run a layered self-test of the broker connection.

    Used by the Broker Status page's "Diagnose" button. Walks settings,
    host resolution, TCP reachability, the FastAPI lifespan client, and
    the account sentinel; returns one row per check with a remediation
    hint when something is not passing. Read-only — does not call
    ``connect()`` and does not place orders.

    When the broker is disabled (``IBKR_BROKER_ENABLED=false``) returns a
    :class:`DiagnosticReportDisabled` sentinel immediately without probing.
    """
    if _is_broker_disabled():
        return DiagnosticReportDisabled(
            disabled=True,
            reason="IBKR_BROKER_ENABLED=false — host-venv runner owns the IBKR session",
            since_ms=_BROKER_DISABLED_SINCE_MS,
        )
    return await run_diagnostics()


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
            return client.health()
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
        return client.health()


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
    if _is_broker_disabled():
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
        health = await client.connect()
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
    return health


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


# ── /account (Phase 2a) ────────────────────────────────────────────────


@router.get("/account", response_model=IbkrAccountSummary)
async def account_summary_endpoint() -> IbkrAccountSummary:
    """One-shot account summary: cash, NLV, margin, account-level P&L.

    Phase 2c — every snapshot is offered to the configured account
    writer (no-op unless ``IBKR_PERSIST_ACCOUNT=true``).
    """
    client = _require_connected_or_503()
    try:
        snapshot = await ibkr_account.fetch_account_summary(client)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    writer = make_account_writer(
        persist=client.settings.persist_account,
        persist_dir=client.settings.persist_dir,
    )
    try:
        await writer.write(snapshot)
        await writer.flush()
    finally:
        await writer.close()
    return snapshot


# ── /positions (Phase 2a) ──────────────────────────────────────────────


@router.get("/positions", response_model=IbkrPositionsSnapshot)
async def positions_endpoint() -> IbkrPositionsSnapshot:
    """All open positions for the connected account."""
    client = _require_connected_or_503()
    try:
        return await ibkr_account.fetch_positions(client)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


# ── /expirations ───────────────────────────────────────────────────────


@router.get("/expirations/{symbol}")
async def list_expirations_endpoint(symbol: str) -> dict:
    client = _require_connected_or_503()
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

    client = _require_connected_or_503()
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


# ── /symbols/search ────────────────────────────────────────────────────


@router.get("/symbols/search")
async def symbols_search_endpoint(
    q: Annotated[str, Query(min_length=1, max_length=32, description="Symbol pattern.")],
    sec_type: Annotated[
        str | None,
        Query(description="Optional secType filter: STK, OPT, FUT, FOP, IND, CASH, etc."),
    ] = None,
) -> dict:
    """Slice 1F — IBKR ``reqMatchingSymbols`` proxy for the cockpit's
    leg picker. Token-bucket rate-limited per ``(q, sec_type)`` at the
    IBKR-published ~1 req/5s ceiling; 60s TTL cache short-circuits
    repeated patterns without consulting the bucket.
    """
    # Canonicalize before keying so " SPY " and "SPY" share one cache /
    # throttle slot, and treat an empty ``sec_type`` query string as
    # "no filter" (FastAPI will otherwise pass it through to the
    # wrapper as an empty literal that drops every row).
    client = _require_connected_or_503()
    q_norm = q.strip()
    sec_type_norm = sec_type if sec_type else None
    key = (q_norm, sec_type_norm)
    cached = _SYMBOL_SEARCH_CACHE.get(key)
    if cached is not None:
        return {"matches": [m.model_dump() for m in cached]}

    # Single global key — IBKR's ``reqMatchingSymbols`` ceiling is per
    # connection, not per pattern. Per-pattern keys would let a fast
    # typist drain N quotas while still tripping the upstream limit.
    retry_after = _SYMBOL_SEARCH_BUCKET.try_acquire("ibkr")
    if retry_after > 0.0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Symbol search rate limit exceeded; retry shortly.",
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )

    try:
        matches = await search_symbols(client, q_norm, sec_type=sec_type_norm)
    except NotConnectedError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "IBKR client not connected.",
        ) from exc

    _SYMBOL_SEARCH_CACHE.set(key, matches)
    return {"matches": [m.model_dump() for m in matches]}


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
    client = _require_connected_or_503()
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

    client = _require_connected_or_503()
    sym = symbol.upper()
    band = sorted(set(float(k) for k in strikes))

    writer = make_writer(
        persist=client.settings.persist_ticks,
        persist_dir=client.settings.persist_dir,
    )
    debounce_seconds = debounce_ms / 1000.0

    async def event_source():
        try:
            async for snapshot in stream_option_chain(client, sym, expiry_ms, band, debounce_seconds=debounce_seconds):
                await writer.write(snapshot)
                payload = _snapshot_to_json(snapshot)
                yield f"event: chain\ndata: {payload}\n\n"
        except asyncio.CancelledError:
            raise
        except BrokerError as exc:
            logger.error("Broker error in option-chain stream: %s", exc)
            err = json.dumps({"error": str(exc)})
            yield f"event: error\ndata: {err}\n\n"
        except ValueError as exc:
            # Contract qualification (``qualify_underlying``,
            # ``build_option_contract``) raises ValueError when IBKR
            # cannot resolve a symbol/strike/right combination — surface
            # those through the same SSE error path as broker errors.
            logger.error("Invalid option-chain request: %s", exc)
            err = json.dumps({"error": str(exc)})
            yield f"event: error\ndata: {err}\n\n"
        finally:
            await writer.close()

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
                "Hard cap on streaming market-data lines. Default 100 "
                "matches IBKR's documented per-client quota; do not raise "
                "without confirming the gateway has been granted more."
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

    client = _require_connected_or_503()
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
            err = json.dumps({"error": str(exc)})
            yield f"event: error\ndata: {err}\n\n"
        except ValueError as exc:
            logger.error("Invalid option-surface request: %s", exc)
            err = json.dumps({"error": str(exc)})
            yield f"event: error\ndata: {err}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── /pnl/stream and /pnl/positions/stream (Phase 2b + 2c) ──────────────


def _pnl_tick_to_sse(tick: IbkrPnLTick) -> str:
    return f"event: pnl\ndata: {tick.model_dump_json()}\n\n"


@router.get("/pnl/stream")
async def pnl_account_stream(
    debounce_ms: Annotated[int, Query(ge=200, le=10_000)] = int(DEFAULT_PNL_DEBOUNCE_S * 1000),
) -> StreamingResponse:
    """Account-level P&L SSE stream."""
    client = _require_connected_or_503()
    debounce_seconds = debounce_ms / 1000.0
    pnl_writer = make_pnl_writer(persist=client.settings.persist_pnl, persist_dir=client.settings.persist_dir)

    async def event_source():
        try:
            async for tick in stream_account_pnl(client, debounce_seconds=debounce_seconds):
                await pnl_writer.write(tick)
                yield _pnl_tick_to_sse(tick)
        except asyncio.CancelledError:
            raise
        except BrokerError as exc:
            logger.error("Broker error in account-pnl stream: %s", exc)
            err = json.dumps({"error": str(exc)})
            yield f"event: error\ndata: {err}\n\n"
        finally:
            await pnl_writer.close()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/pnl/positions/stream")
async def pnl_positions_stream(
    con_ids: Annotated[list[int] | None, Query()] = None,
    debounce_ms: Annotated[int, Query(ge=200, le=10_000)] = int(DEFAULT_PNL_DEBOUNCE_S * 1000),
) -> StreamingResponse:
    """Per-position P&L SSE stream.

    ``con_ids`` is a repeated query parameter. We accept it as optional
    and reject missingness explicitly with 422 — declaring it required
    via ``Query()`` triggers a known FastAPI 0.104 / Pydantic 2 encoder
    bug where the missing-value validation error contains
    ``PydanticUndefined`` and fails JSON serialisation, surfacing as a
    500 instead of the documented 422.
    """
    if not con_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "con_ids must be non-empty.")

    client = _require_connected_or_503()
    debounce_seconds = debounce_ms / 1000.0
    pnl_writer = make_pnl_writer(persist=client.settings.persist_pnl, persist_dir=client.settings.persist_dir)

    async def event_source():
        try:
            async for tick in stream_position_pnl(client, con_ids, debounce_seconds=debounce_seconds):
                await pnl_writer.write(tick)
                yield _pnl_tick_to_sse(tick)
        except asyncio.CancelledError:
            raise
        except BrokerError as exc:
            logger.error("Broker error in positions-pnl stream: %s", exc)
            err = json.dumps({"error": str(exc)})
            yield f"event: error\ndata: {err}\n\n"
        finally:
            await pnl_writer.close()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── /orders POST (Phase 3a) ────────────────────────────────────────────


@router.post("/orders", response_model=IbkrOrderAck, status_code=status.HTTP_201_CREATED)
async def place_order_endpoint(spec: IbkrOrderSpec) -> IbkrOrderAck:
    """Place one paper order. Four safety layers run before any IBKR call."""
    client = _require_connected_or_503()
    try:
        return await place_paper_order(client, spec)
    except OrderRefusedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


# ── /orders open + cancel + stream (Phase 3b) ──────────────────────────


@router.get("/orders/open", response_model=list[IbkrOpenOrder])
async def list_open_orders_endpoint() -> list[IbkrOpenOrder]:
    """All open orders the connected paper-client has placed."""
    client = _require_connected_or_503()
    try:
        return await list_open_orders(client)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.delete(
    "/orders/{order_id}",
    response_model=IbkrOpenOrder,
    status_code=status.HTTP_200_OK,
)
async def cancel_order_endpoint(order_id: int) -> IbkrOpenOrder:
    """Cancel one paper order by ``order_id``."""
    client = _require_connected_or_503()
    try:
        return await cancel_paper_order(client, order_id)
    except OrderRefusedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except OrderNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


def _order_event_to_sse(event: IbkrOrderEvent) -> str:
    return f"event: order\ndata: {event.model_dump_json()}\n\n"


@router.get("/orders/stream")
async def order_events_stream_endpoint(
    poll_ms: Annotated[int, Query(ge=100, le=5000)] = 500,
) -> StreamingResponse:
    """SSE stream of order lifecycle events: status, fill, cancel, error."""
    client = _require_connected_or_503()
    poll_seconds = poll_ms / 1000.0

    async def event_source():
        try:
            async for event in stream_order_events(client, poll_seconds=poll_seconds):
                yield _order_event_to_sse(event)
        except asyncio.CancelledError:
            raise
        except BrokerError as exc:
            logger.error("Broker error in order-event stream: %s", exc)
            err = json.dumps({"error": str(exc)})
            yield f"event: error\ndata: {err}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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

    Mirror of ``/bars/snapshot`` for the high-resolution chart. Opens its
    own ``reqRealTimeBars`` subscription independent of the 1-min one;
    they share no state. Each bar's ``end_ms - start_ms`` window is 5 000.
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


def _is_broker_disabled() -> bool:
    from app.broker.ibkr.config import get_settings

    return not get_settings().broker_enabled


def _require_connected_or_503():
    if _is_broker_disabled():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "IBKR broker is disabled (IBKR_BROKER_ENABLED=false). Use /api/live-runs for paper-run status.",
        )
    try:
        client = get_client()
    except NotConnectedError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "IBKR client not initialised.",
        ) from exc
    if not client.is_connected():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "IBKR client not connected to Gateway.",
        )
    return client


def _snapshot_to_json(snapshot: IbkrChainSnapshot) -> str:
    return snapshot.model_dump_json()


def _surface_snapshot_to_json(snapshot: IbkrSurfaceSnapshot) -> str:
    return snapshot.model_dump_json()
