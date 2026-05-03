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
from datetime import UTC, datetime

from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.models import IbkrPnLTick, _coerce_optional_float

logger = logging.getLogger(__name__)


DEFAULT_PNL_DEBOUNCE_S = 1.0


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


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
        ts_ms=_now_ms(),
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
        ts_ms=_now_ms(),
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
    logger.info("Subscribed to account P&L for %s", account_id)

    try:
        # Emit the initial snapshot, then debounce-poll.
        yield _account_pnl_to_tick(pnl, account_id)
        while True:
            await asyncio.sleep(debounce_seconds)
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
    * Don't oversubscribe — IBKR's per-client streaming-line quota is
      shared with market-data subscriptions.
    """
    client.require_connected()
    account_id = client.connected_account
    if account_id is None:
        raise RuntimeError("connected client has no account_id")

    # Subscribe once per contract; keep handles so we can cancel on exit.
    subscriptions: dict[int, object] = {}
    for con_id in con_ids:
        try:
            subscriptions[con_id] = client.ib.reqPnLSingle(account_id, "", con_id)
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
            for con_id, pnl_single in subscriptions.items():
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
