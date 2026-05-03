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
    IbkrPositionsSnapshot,
)
from app.broker.ibkr.persistence import make_writer

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
    """
    client = _require_connected_or_503()
    try:
        return await ibkr_account.fetch_account_summary(client)
    except BrokerError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


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
