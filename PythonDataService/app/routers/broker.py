"""Public broker endpoints (curated subset).

This is the *only* place outside ``app.broker.*`` that touches IBKR.
The .NET backend and Angular frontend reach IBKR via these endpoints —
no tight coupling to ``ib_async`` types crosses this boundary.

Endpoints:

* ``GET /api/broker/health`` — connection status + paper/live sentinel
  result. Always 200; the body's ``connected=False`` carries the
  disconnected case.
* ``GET /api/broker/expirations/{symbol}`` — list available expiries.
* ``GET /api/broker/option-chain/{symbol}`` — Server-Sent Events stream
  of ``IbkrChainSnapshot`` JSON, one event per debounce window.

Phase 2 (out of scope here): ``/positions``, ``/pnl-stream``.
Phase 3 (out of scope here): ``POST /orders``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.broker.ibkr import account as ibkr_account
from app.broker.ibkr import contracts as ibkr_contracts
from app.broker.ibkr.client import (
    BrokerError,
    NotConnectedError,
    get_client,
)
from app.broker.ibkr.market_data import stream_option_chain
from app.broker.ibkr.models import (
    IbkrAccountSummary,
    IbkrChainSnapshot,
    IbkrConnectionHealth,
    IbkrOrderAck,
    IbkrOrderSpec,
    IbkrPnLTick,
    IbkrPositionsSnapshot,
)
from app.broker.ibkr.orders import (
    OrderRefusedError,
    place_paper_order,
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

router = APIRouter(prefix="/api/broker", tags=["broker"])
logger = logging.getLogger(__name__)


# ── /health ─────────────────────────────────────────────────────────────


@router.get("/health", response_model=IbkrConnectionHealth)
async def broker_health() -> IbkrConnectionHealth:
    """Connection diagnostic. Never raises on disconnect.

    The Angular banner that renders the paper/live pill reads from
    ``mode`` and ``is_paper``. Phase 1 contract: if ``connected=False``
    the UI shows a yellow "BROKER DISCONNECTED" toast.
    """
    try:
        client = get_client()
    except NotConnectedError:
        # Lifespan never initialised the client — surface a synthetic
        # disconnected payload rather than 500. The settings module is
        # the source of truth for what "should have connected to" means.
        from datetime import UTC, datetime

        from app.broker.ibkr.config import get_settings

        s = get_settings()
        return IbkrConnectionHealth(
            mode=s.mode,
            host=s.host,
            port=s.port,
            client_id=s.client_id,
            connected=False,
            account_id=None,
            is_paper=None,
            server_version=None,
            fetched_at_ms=int(datetime.now(tz=UTC).timestamp() * 1000),
        )
    return client.health()


# ── /account (Phase 2a) ─────────────────────────────────────────────────


@router.get("/account", response_model=IbkrAccountSummary)
async def account_summary_endpoint() -> IbkrAccountSummary:
    """One-shot account summary: cash, NLV, margin, account-level P&L.

    Phase 2a is sync (single round-trip). Phase 2b adds the SSE P&L
    stream for live day-P&L; this endpoint is for the
    "what's my account state right now" landing card.

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


# ── /positions (Phase 2a) ───────────────────────────────────────────────


@router.get("/positions", response_model=IbkrPositionsSnapshot)
async def positions_endpoint() -> IbkrPositionsSnapshot:
    """All open positions for the connected account.

    Phase 2a returns a flat list (stocks + options share one model).
    Multi-leg strategy grouping is a Phase 2.5 follow-up; for now the
    UI groups visually but the wire is raw legs.
    """
    client = _require_connected_or_503()
    try:
        return await ibkr_account.fetch_positions(client)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


# ── /expirations ────────────────────────────────────────────────────────


@router.get("/expirations/{symbol}")
async def list_expirations_endpoint(symbol: str) -> dict:
    client = _require_connected_or_503()
    try:
        expirations = await ibkr_contracts.list_expirations(client, symbol.upper())
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"symbol": symbol.upper(), "expirations_ms": expirations}


# ── /option-chain (SSE) ─────────────────────────────────────────────────


@router.get("/option-chain/{symbol}")
async def option_chain_stream(
    symbol: str,
    expiry_ms: Annotated[int, Query(..., description="Expiry timestamp in int64 ms UTC.")],
    strike_min: Annotated[float, Query(..., gt=0, description="Lower strike bound.")],
    strike_max: Annotated[float, Query(..., gt=0, description="Upper strike bound.")],
    debounce_ms: Annotated[int, Query(ge=50, le=5000)] = 250,
) -> StreamingResponse:
    """SSE stream of chain snapshots.

    The caller narrows strikes via [strike_min, strike_max] — one expiry,
    one band, one stream. Snapshot interval is the debounce window;
    typical clients pick 250 ms.

    The strike list inside the band is fetched from IBKR once at
    subscription time. Strikes added or removed by IBKR mid-stream are
    not picked up — clients reconnect on a noticeable absence.
    """
    if strike_max < strike_min:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "strike_max must be >= strike_min.",
        )

    client = _require_connected_or_503()
    sym = symbol.upper()
    all_strikes = await ibkr_contracts.list_strikes(client, sym, expiry_ms)
    band = [k for k in all_strikes if strike_min <= k <= strike_max]
    if not band:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No strikes between {strike_min} and {strike_max} for {sym} expiry={expiry_ms}.",
        )

    writer = make_writer(
        persist=client.settings.persist_ticks,
        persist_dir=client.settings.persist_dir,
    )
    debounce_seconds = debounce_ms / 1000.0

    async def event_source():
        try:
            async for snapshot in stream_option_chain(
                client,
                sym,
                expiry_ms,
                band,
                debounce_seconds=debounce_seconds,
            ):
                await writer.write(snapshot)
                payload = _snapshot_to_json(snapshot)
                yield f"event: chain\ndata: {payload}\n\n"
        except asyncio.CancelledError:
            # Consumer disconnected. SSE generators are expected to
            # propagate cancellation; the stream's ``finally`` cancels
            # IBKR subscriptions.
            raise
        except BrokerError as exc:
            logger.error("Broker error in option-chain stream: %s", exc)
            err = json.dumps({"error": str(exc)})
            yield f"event: error\ndata: {err}\n\n"
        finally:
            await writer.close()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable buffering on nginx-style proxies
        },
    )


# ── /pnl/stream and /pnl/positions/stream (Phase 2b) ────────────────────


def _pnl_tick_to_sse(tick: IbkrPnLTick) -> str:
    return f"event: pnl\ndata: {tick.model_dump_json()}\n\n"


@router.get("/pnl/stream")
async def pnl_account_stream(
    debounce_ms: Annotated[int, Query(ge=200, le=10_000)] = int(DEFAULT_PNL_DEBOUNCE_S * 1000),
) -> StreamingResponse:
    """SSE stream of account-level P&L ticks.

    Each event carries an ``IbkrPnLTick`` with ``con_id=None`` and
    populated daily/unrealized/realized P&L. Consumer gets a tick on
    subscribe (the initial PnL snapshot) plus one per debounce window.
    """
    client = _require_connected_or_503()
    debounce_seconds = debounce_ms / 1000.0
    pnl_writer = make_pnl_writer(
        persist=client.settings.persist_pnl,
        persist_dir=client.settings.persist_dir,
    )

    async def event_source():
        try:
            async for tick in stream_account_pnl(
                client,
                debounce_seconds=debounce_seconds,
            ):
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
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/pnl/positions/stream")
async def pnl_positions_stream(
    con_ids: Annotated[
        list[int],
        Query(
            ...,
            description="One or more IBKR conIds to subscribe to (?con_ids=123&con_ids=456).",
        ),
    ],
    debounce_ms: Annotated[int, Query(ge=200, le=10_000)] = int(DEFAULT_PNL_DEBOUNCE_S * 1000),
) -> StreamingResponse:
    """SSE stream of per-position P&L ticks for the requested contracts.

    Caller pre-resolves contract IDs via ``GET /api/broker/positions``
    (Phase 2a). One event per (contract × debounce window). Use the
    ``con_id`` field on each tick to demultiplex on the client.
    """
    if not con_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "con_ids must be non-empty.")

    client = _require_connected_or_503()
    debounce_seconds = debounce_ms / 1000.0
    pnl_writer = make_pnl_writer(
        persist=client.settings.persist_pnl,
        persist_dir=client.settings.persist_dir,
    )

    async def event_source():
        try:
            async for tick in stream_position_pnl(
                client,
                con_ids,
                debounce_seconds=debounce_seconds,
            ):
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
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── /orders (Phase 3a) — paper-only POST ────────────────────────────────


@router.post("/orders", response_model=IbkrOrderAck, status_code=status.HTTP_201_CREATED)
async def place_order_endpoint(spec: IbkrOrderSpec) -> IbkrOrderAck:
    """Place one paper order.

    All four safety layers run before any IBKR call:

    1. ``IBKR_MODE=paper`` (env var, validated at config time).
    2. Connected port is NOT a known live port.
    3. Connected account id begins with ``DU``.
    4. ``spec.confirm_paper`` is ``True``.

    Any failure → ``HTTP 403`` and no broker call. Other broker errors
    (contract qualification, etc.) → ``502``. Successful submission
    returns the broker-assigned ``orderId`` and ``permId`` in the body.

    Phase 3a supports MKT and LMT only on STK and OPT. Brackets, OCO,
    cancels, and the order event stream are Phase 3b.
    """
    client = _require_connected_or_503()
    try:
        return await place_paper_order(client, spec)
    except OrderRefusedError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


# ── helpers ─────────────────────────────────────────────────────────────


def _require_connected_or_503():
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
    """Pydantic v2 ``.model_dump_json`` keeps numeric None correct."""
    return snapshot.model_dump_json()
