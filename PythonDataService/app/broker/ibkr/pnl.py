"""Real-time P&L streams.

Phase 2b surface (async iterators, debounced):

* ``stream_account_pnl(client)`` yields one ``IbkrPnLTick`` per debounce
  window with the account-level day P&L, unrealised, and realised.
* ``stream_position_pnl(client, con_ids)`` yields one ``IbkrPnLTick``
  per (con_id, debounce window). The first iteration emits one tick
  per requested contract; subsequent iterations only emit ticks that
  have updated since the last yield.

Both wrap ib_async's PnL subscriptions (``reqPnL`` / ``reqPnLSingle``)
and cancel them in ``finally`` so the consumer disconnecting doesn't
leak server-side subscriptions.

The debounce / coalesce pattern mirrors ``market_data.stream_option_chain``
so SSE consumers see the same shape across Phase 1 and Phase 2.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from app.broker.ibkr.api_evidence import (
    evidence_request,
    evidence_response,
    get_ibkr_api_evidence_recorder,
)
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.models import IbkrPnLTick, _coerce_optional_float
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


DEFAULT_PNL_DEBOUNCE_S = 1.0


def _account_pnl_to_tick(pnl, account_id: str) -> IbkrPnLTick:
    """Convert ib_async ``PnL`` (account-level) to our wire model."""
    return IbkrPnLTick(
        account_id=account_id,
        con_id=None,
        daily_pnl=_coerce_optional_float(getattr(pnl, "dailyPnL", None)),
        unrealized_pnl=_coerce_optional_float(getattr(pnl, "unrealizedPnL", None)),
        realized_pnl=_coerce_optional_float(getattr(pnl, "realizedPnL", None)),
        market_value=None,
        position=None,
        ts_ms=now_ms_utc(),
    )


def _position_pnl_to_tick(
    pnl_single,
    account_id: str,
    con_id: int,
) -> IbkrPnLTick:
    """Convert ib_async ``PnLSingle`` (per-position) to our wire model."""
    return IbkrPnLTick(
        account_id=account_id,
        con_id=con_id,
        daily_pnl=_coerce_optional_float(getattr(pnl_single, "dailyPnL", None)),
        unrealized_pnl=_coerce_optional_float(getattr(pnl_single, "unrealizedPnL", None)),
        realized_pnl=_coerce_optional_float(getattr(pnl_single, "realizedPnL", None)),
        market_value=_coerce_optional_float(getattr(pnl_single, "value", None)),
        position=_coerce_optional_float(getattr(pnl_single, "position", None)),
        ts_ms=now_ms_utc(),
    )


async def stream_account_pnl(
    client: IbkrClient,
    *,
    debounce_seconds: float = DEFAULT_PNL_DEBOUNCE_S,
) -> AsyncIterator[IbkrPnLTick]:
    """Yield account-level P&L ticks until the consumer disconnects.

    The first iteration emits a tick from the initial ``PnL`` snapshot
    even if no events have fired yet — IBKR populates the object
    immediately after subscribe. Subsequent iterations re-read the same
    object after ``debounce_seconds`` of accumulated events.
    """
    client.require_connected()
    account_id = client.connected_account
    if account_id is None:
        raise RuntimeError("connected client has no account_id")

    # ``reqPnL`` returns a PnL object whose fields update in place as the
    # broker streams events. We keep a handle to cancel on exit.
    pnl = client.ib.reqPnL(account_id)
    recorder = get_ibkr_api_evidence_recorder()
    recorder.record(
        source="pnl.stream_account_pnl.subscribe",
        account_id=account_id,
        request=evidence_request("reqPnL", account=account_id, modelCode=""),
        response=evidence_response("pnl", objects=[pnl]),
    )
    logger.info("Subscribed to account P&L for %s", account_id)

    try:
        # Emit the initial snapshot, then debounce-poll.
        yield _account_pnl_to_tick(pnl, account_id)
        while True:
            await asyncio.sleep(debounce_seconds)
            # ib_async mutates ``pnl`` in place; on a disconnect it simply stops
            # updating, so re-reading it would emit plausible-but-frozen P&L
            # forever. Halt instead of streaming stale risk numbers.
            client.require_live()
            recorder.record(
                source="pnl.stream_account_pnl.tick",
                account_id=account_id,
                request=evidence_request("reqPnL", account=account_id, modelCode=""),
                response=evidence_response("pnl", objects=[pnl]),
            )
            yield _account_pnl_to_tick(pnl, account_id)
    finally:
        try:
            client.ib.cancelPnL(account_id)
        except Exception as exc:
            logger.debug("cancelPnL(%s) raised on shutdown: %s", account_id, exc)


async def stream_position_pnl(
    client: IbkrClient,
    con_ids: list[int],
    *,
    debounce_seconds: float = DEFAULT_PNL_DEBOUNCE_S,
) -> AsyncIterator[IbkrPnLTick]:
    """Yield per-position P&L ticks for every requested contract.

    Subscribes one ``reqPnLSingle`` per ``con_id``. Per debounce window
    the iterator emits a tick for every position (one yield per tick).
    Use ``con_id`` on the tick to demultiplex.

    Caller responsibilities:
    * Pre-resolve ``con_ids`` from ``fetch_positions`` (Phase 2a).
    * Bound the request set to current positions and cancel on exit. IBKR's
      published P&L documentation does not identify ``reqPnLSingle`` as a
      Level-I market-data-line consumer, so do not conflate this set with the
      user-level market-data allocation.
    """
    client.require_connected()
    account_id = client.connected_account
    if account_id is None:
        raise RuntimeError("connected client has no account_id")

    # Subscribe once per contract; keep handles so we can cancel on exit.
    subscriptions: dict[int, object] = {}
    recorder = get_ibkr_api_evidence_recorder()
    for con_id in con_ids:
        try:
            subscriptions[con_id] = client.ib.reqPnLSingle(account_id, "", con_id)
            recorder.record(
                source="pnl.stream_position_pnl.subscribe",
                account_id=account_id,
                request=evidence_request(
                    "reqPnLSingle",
                    account=account_id,
                    modelCode="",
                    conId=int(con_id),
                ),
                response=evidence_response("pnlSingle", objects=[subscriptions[con_id]]),
            )
        except Exception as exc:
            logger.warning("reqPnLSingle failed for con_id=%s: %s", con_id, exc)

    logger.info(
        "Subscribed to per-position P&L for %d contract(s) on %s",
        len(subscriptions),
        account_id,
    )

    try:
        # First-pass emit so consumers see initial values without waiting.
        for con_id, pnl_single in subscriptions.items():
            yield _position_pnl_to_tick(pnl_single, account_id, con_id)
        while True:
            await asyncio.sleep(debounce_seconds)
            # See stream_account_pnl: a disconnect freezes these PnLSingle
            # objects, so halt rather than emit stale per-position risk.
            client.require_live()
            for con_id, pnl_single in subscriptions.items():
                recorder.record(
                    source="pnl.stream_position_pnl.tick",
                    account_id=account_id,
                    request=evidence_request(
                        "reqPnLSingle",
                        account=account_id,
                        modelCode="",
                        conId=int(con_id),
                    ),
                    response=evidence_response("pnlSingle", objects=[pnl_single]),
                )
                yield _position_pnl_to_tick(pnl_single, account_id, con_id)
    finally:
        for con_id in subscriptions:
            try:
                client.ib.cancelPnLSingle(account_id, "", con_id)
            except Exception as exc:
                logger.debug(
                    "cancelPnLSingle(%s, %s) raised on shutdown: %s",
                    account_id,
                    con_id,
                    exc,
                )
