"""Public broker endpoints (curated subset).

This is the *only* place outside ``app.broker.*`` that touches IBKR.
The .NET backend and Angular frontend reach IBKR via these endpoints —
no tight coupling to ``ib_async`` types crosses this boundary.

Endpoints (Phase 1 + Phase 2 + Phase 3):

* ``GET /api/broker/health`` — connection + sentinel.
* ``GET /api/broker/expirations/{symbol}`` — list expiries.
* ``GET /api/broker/option-chain/{symbol}`` — SSE chain stream.
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
from app.broker.ibkr.diagnostics import run_diagnostics
from app.broker.ibkr.market_data import stream_option_chain
from app.broker.ibkr.models import (
    DiagnosticReport,
    IbkrAccountSummary,
    IbkrChainSnapshot,
    IbkrConnectionHealth,
    IbkrOpenOrder,
    IbkrOrderAck,
    IbkrOrderEvent,
    IbkrOrderSpec,
    IbkrPnLTick,
    IbkrPositionsSnapshot,
    IbkrStrikeList,
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

router = APIRouter(prefix="/api/broker", tags=["broker"])
logger = logging.getLogger(__name__)


# ── /health ────────────────────────────────────────────────────────────


@router.get("/health", response_model=IbkrConnectionHealth)
async def broker_health() -> IbkrConnectionHealth:
    """Connection diagnostic. Never raises on disconnect."""
    try:
        client = get_client()
    except NotConnectedError:
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


# ── /diagnose ──────────────────────────────────────────────────────────


@router.get("/diagnose", response_model=DiagnosticReport)
async def broker_diagnose() -> DiagnosticReport:
    """Run a layered self-test of the broker connection.

    Used by the Broker Status page's "Diagnose" button. Walks settings,
    host resolution, TCP reachability, the FastAPI lifespan client, and
    the account sentinel; returns one row per check with a remediation
    hint when something is not passing. Read-only — does not call
    ``connect()`` and does not place orders.
    """
    return await run_diagnostics()


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
    expiry_ms: Annotated[int, Query(..., description="Expiry timestamp in int64 ms UTC.")],
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


# ── /option-chain (SSE) ────────────────────────────────────────────────


@router.get("/option-chain/{symbol}")
async def option_chain_stream(
    symbol: str,
    expiry_ms: Annotated[int, Query(..., description="Expiry timestamp in int64 ms UTC.")],
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
            async for snapshot in stream_option_chain(
                client, sym, expiry_ms, band, debounce_seconds=debounce_seconds
            ):
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
    pnl_writer = make_pnl_writer(
        persist=client.settings.persist_pnl, persist_dir=client.settings.persist_dir
    )

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
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "con_ids must be non-empty."
        )

    client = _require_connected_or_503()
    debounce_seconds = debounce_ms / 1000.0
    pnl_writer = make_pnl_writer(
        persist=client.settings.persist_pnl, persist_dir=client.settings.persist_dir
    )

    async def event_source():
        try:
            async for tick in stream_position_pnl(
                client, con_ids, debounce_seconds=debounce_seconds
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


# ── helpers ────────────────────────────────────────────────────────────


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
    return snapshot.model_dump_json()
