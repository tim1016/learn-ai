"""IBKR order-history sweeps for terminal broker order states."""

from __future__ import annotations

import asyncio
import logging

from pydantic import ValidationError

from app.broker.ibkr.api_evidence import (
    evidence_request,
    evidence_response,
    get_ibkr_api_evidence_recorder,
)
from app.broker.ibkr.client import BrokerError, IbkrClient
from app.broker.ibkr.models import IbkrOpenOrder
from app.broker.ibkr.order_projection import order_belongs_to_account
from app.broker.ibkr.orders import _trade_to_open_order

logger = logging.getLogger(__name__)

_COMPLETED_ORDERS_TIMEOUT_S = 30.0


async def list_completed_orders(
    client: IbkrClient,
    *,
    api_only: bool = False,
) -> list[IbkrOpenOrder]:
    """Return recently completed/cancelled/rejected orders visible to TWS.

    IBKR's completed-order surface is a recent live API view, not the official
    historical statement. Flex remains the delayed audit source for settled
    history.
    """
    client.require_connected()
    account_id = client.connected_account
    if account_id is None:
        raise BrokerError("connected client has no account_id")

    request = evidence_request("reqCompletedOrdersAsync", api_only=api_only)
    completed_orders_call = getattr(client.ib, "reqCompletedOrdersAsync", None)
    if not callable(completed_orders_call):
        raise BrokerError("IBKR reqCompletedOrdersAsync is unavailable on this client.")
    try:
        trades = await asyncio.wait_for(
            completed_orders_call(api_only),
            timeout=_COMPLETED_ORDERS_TIMEOUT_S,
        )
    except AttributeError as exc:
        raise BrokerError(
            f"IBKR reqCompletedOrdersAsync is unavailable on this client: {exc}"
        ) from exc
    except TimeoutError as exc:
        raise BrokerError(
            f"IBKR reqCompletedOrdersAsync timed out after "
            f"{_COMPLETED_ORDERS_TIMEOUT_S:.0f}s; completed-order evidence is stale."
        ) from exc

    get_ibkr_api_evidence_recorder().record(
        source="order_history.list_completed_orders",
        account_id=account_id,
        request=request,
        response=evidence_response(
            "completedOrder",
            fields={"trade_count": len(trades), "api_only": api_only},
            objects=trades,
        ),
    )

    out: list[IbkrOpenOrder] = []
    parse_error_count = 0
    for trade in trades:
        if not order_belongs_to_account(trade, account_id):
            continue
        try:
            out.append(
                _trade_to_open_order(
                    trade,
                    account_id,
                    client.settings.client_id,
                    request=request,
                    response_callback="completedOrder",
                )
            )
        except (AttributeError, TypeError, ValueError, ValidationError) as exc:
            parse_error_count += 1
            logger.warning(
                "Skipping unparseable completed order conId=%s: %s",
                getattr(getattr(trade, "contract", None), "conId", "?"),
                exc,
            )
    if parse_error_count:
        raise BrokerError(
            "IBKR completed-order sweep contained "
            f"{parse_error_count} unparseable row(s); completed-order evidence is degraded."
        )
    return out
